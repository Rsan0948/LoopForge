"""Offline regressions for authority handoffs discovered by the fresh audit."""

import hashlib
import inspect
import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from test_repository_investigation import config as config_fixture

from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.adapters.research_store import ResearchStore
from loopforge.application.failure_recovery import worker_failure
from loopforge.application.supervisor import GoalCoordinator
from loopforge.domain.events import ModelAttemptFailed
from loopforge.domain.goal_review import (
    ClarificationPolicy,
    ControlAcknowledged,
    ExperimentRecorded,
    ExperimentState,
    QuestionAnswered,
    QuestionOpened,
    QuestionState,
)
from loopforge.domain.goals import (
    GoalAction,
    GoalCreated,
    GoalEvent,
    GoalPolicy,
    GoalState,
    GoalStopped,
    SupervisorDecision,
    WorkerObservation,
    reduce_goal,
)
from loopforge.domain.investigation import (
    FailureKind,
    InspectionRequest,
    ResearchRecord,
    TaskAssignment,
)
from loopforge.domain.types import EventId, RunId
from loopforge.entrypoints.campaign_coding import patch_identity, prepare_checkout, writable
from loopforge.entrypoints.campaign_models import CodingTaskConfig
from loopforge.entrypoints.goal_checkpoint import snapshot_checkpoint
from loopforge.entrypoints.goal_investigation import RepositoryInvestigation
from loopforge.entrypoints.goal_review import morning_report
from loopforge.entrypoints.goal_runner import record_runner, runner_record
from loopforge.entrypoints.goal_successor import prepare_successor
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL, BaseGoalService
from loopforge.ports.supervisor import SupervisorRequest, SupervisorResponse


@pytest.fixture
def repository_config(tmp_path: Path) -> RepositoryGoalConfig:
    return cast("Callable[[Path], RepositoryGoalConfig]", inspect.unwrap(config_fixture))(tmp_path)


def investigation(config: RepositoryGoalConfig, directory: Path) -> RepositoryInvestigation:
    return RepositoryInvestigation(
        config, directory, ResearchStore(SQLiteGoalJournal(directory / "research.sqlite"))
    )


def ready_state(config: RepositoryGoalConfig) -> GoalState:
    return GoalState(
        policy=GoalPolicy(
            "Implement the approved feature",
            "a" * 64,
            500000,
            1000,
            checkpoint_revision=config.campaign.revision,
        ),
        environment_ready=True,
        baseline_reproduced=True,
        environment_id="stdlib",
        research=(
            ResearchRecord(
                "e_source",
                "inspect",
                "source",
                config.campaign.revision,
                "src/parse.py",
                "a" * 64,
                "Observed source",
                "def parse(value): return value",
            ),
        ),
    )


def test_narrow_assignment_preserves_only_relevant_approved_maintenance(
    repository_config: RepositoryGoalConfig, tmp_path: Path
) -> None:
    values = repository_config.model_dump()
    values["task"]["editable_project_paths"] = ["src/parse.py", "src/sum.py"]
    config = RepositoryGoalConfig.model_validate(values)
    research = investigation(config, tmp_path / "goal")
    assignment = TaskAssignment(
        "Fix only the parser implementation", ("src/parse.py",), ("e_source",)
    )
    prepared = research._task(assignment, ready_state(config))  # pyright: ignore[reportPrivateUsage] - exercise pre-binding scope validator
    assert prepared.allowed_paths == ["src/parse.py"]
    assert prepared.editable_project_paths == ["src/parse.py"]
    assert prepared.acceptance_probes == config.task.acceptance_probes
    with pytest.raises(ValueError, match="expands authorized"):
        research._task(replace(assignment, files=("outside/file.py",)), ready_state(config))  # pyright: ignore[reportPrivateUsage] - exercise pre-binding scope validator


@pytest.mark.parametrize("classification", ["optional", "permission"])
def test_expired_midtask_question_before_first_verified_candidate(classification: str) -> None:
    topic = ClarificationPolicy(
        "design",
        "Which layout?",
        classification,
        10,
        "Keep existing layout" if classification == "optional" else "",
    )
    policy = GoalPolicy(
        "Implement the approved feature",
        "a" * 64,
        1000,
        1000,
        investigation_enabled=True,
        checkpoint_revision="b" * 40,
        clarifications=(topic,),
    )
    initial = GoalState(
        policy=policy,
        phase="ready",
        assignment=TaskAssignment("Implement approved feature", ("src/a.py",), ("e_source",)),
        environment_ready=True,
        baseline_reproduced=True,
        questions=(QuestionState("design", 90),),
        observation=WorkerObservation("review_pending", "{}", "c" * 64),
    )

    class Journal:
        state = initial

        def load(self, goal: str) -> GoalState:
            assert goal == GOAL
            return self.state

        def append(self, goal: str, sequence: int, event: GoalEvent) -> GoalState:
            assert goal == GOAL
            assert sequence == self.state.sequence
            self.state = reduce_goal(self.state, event)
            return self.state

    class Clock:
        def now(self) -> datetime:
            return datetime.fromtimestamp(100, UTC)

    class Unused:
        def prepare(self, state: GoalState) -> SupervisorRequest:
            del state
            msg = "Unexpected provider call"
            raise AssertionError(msg)

        def decide(self, request: SupervisorRequest) -> SupervisorResponse:
            del request
            msg = "Unexpected provider call"
            raise AssertionError(msg)

        def observe(self) -> WorkerObservation:
            msg = "Unexpected worker call"
            raise AssertionError(msg)

        def reconcile(self) -> WorkerObservation:
            msg = "Unexpected worker call"
            raise AssertionError(msg)

        def execute(self, action: GoalAction, key: str) -> WorkerObservation:
            del action, key
            msg = "Unexpected worker call"
            raise AssertionError(msg)

    result = GoalCoordinator(Journal(), Unused(), Unused(), Clock()).step(GOAL)
    assert result.policy == policy
    if classification == "optional":
        assert len(result.experiments) == 1
        assert result.experiments[0].checkpoint == policy.checkpoint_revision
    else:
        assert result == initial
        with pytest.raises(ValueError, match="expired preauthorized optional"):
            reduce_goal(
                result,
                ExperimentRecorded(
                    "design", "experiment/" + "d" * 20, policy.checkpoint_revision, 100
                ),
            )


def test_experiment_seed_uses_verified_checkpoint_instead_of_newer_failing_candidate(
    repository_config: RepositoryGoalConfig, tmp_path: Path
) -> None:
    config = repository_config
    directory = tmp_path / "goal"
    workspace = prepare_checkout(
        Path(config.campaign.repository),
        config.campaign.revision,
        directory / "current",
        config.task.branch,
    )
    source = workspace.root / "src/parse.py"
    source.write_text("def parse(value): return int(value)\n")
    verified = patch_identity(workspace)
    snapshot_checkpoint(workspace.root, directory / "verified" / verified, workspace.base_revision)
    source.write_text("def parse(value): raise ValueError('new failing hypothesis')\n")
    failing = patch_identity(workspace)
    research = investigation(config, directory)
    research.candidate = lambda: (workspace.root, patch_identity(workspace))
    assignment = TaskAssignment(
        "Repair the parser implementation", ("src/parse.py",), ("e_source",)
    )
    experiment = ExperimentState(
        "design", "experiment/" + "d" * 20, verified, "Preserve current behavior"
    )
    state = replace(
        ready_state(config),
        assignment=assignment,
        experiments=(experiment,),
        last_verified_candidate=verified,
        observation=WorkerObservation("attention", "{}", failing),
    )
    record = research.perform(
        SupervisorDecision(
            GoalAction.DELEGATE,
            "Continue approved experiment",
            "Implement safely",
            assignment=assignment,
        ),
        "experiment-seed",
        state,
    )
    seed = directory / record.candidate_checkpoint / "checkpoint"
    assert (seed / "src/parse.py").read_text() == "def parse(value): return int(value)\n"
    assert source.read_text() == "def parse(value): raise ValueError('new failing hypothesis')\n"
    assert record.candidate_sha256 == failing
    assert record.seed_manifest_sha256


def test_three_generations_share_paid_cap_unknown_reservations_and_reporting(
    tmp_path: Path,
) -> None:
    services: list[BaseGoalService] = []
    try:
        for name, parent in (("root", None), ("child", "root"), ("grandchild", "child")):
            service = BaseGoalService(tmp_path / name)
            services.append(service)
            state = service.journal.append(
                GOAL,
                0,
                GoalCreated(
                    GoalPolicy(
                        "Continue approved feature", "a" * 64, 1000 if parent is None else 900, 1000
                    )
                ),
            )
            service.journal.append(
                GOAL, state.sequence, GoalStopped("blocked", "Retained predecessor")
            )
            if parent:
                (service.directory / "parent-link.json").write_text(
                    json.dumps({"directory": str(tmp_path / parent)})
                )
        root, child, grandchild = services
        budget = root.paid_budget()
        budget.reserve("review:known", "review", 300)
        budget.settle("review:known", 200)
        child.paid_budget().reserve("repair:unknown", "repair", 600)
        inherited = grandchild.paid_budget()
        assert inherited.path == budget.path
        with pytest.raises(ValueError, match="reservation denied"):
            inherited.reserve("supervisor:excess", "supervisor", 201)
        report = morning_report(grandchild.directory)
        assert report["paid_spent_microusd"] == 200
        assert report["paid_reserved_microusd"] == 600
        assert report["remaining_microusd"] == 200
        assert not (child.directory / "paid-budget.sqlite").exists()
        assert not (grandchild.directory / "paid-budget.sqlite").exists()
    finally:
        for service in reversed(services):
            service.close()


def test_approved_hidden_workflow_is_inspectable_assignable_and_writable(
    repository_config: RepositoryGoalConfig, tmp_path: Path
) -> None:
    path = ".github/workflows/ci.yml"
    root = Path(repository_config.campaign.repository)
    (root / path).parent.mkdir(parents=True)
    (root / path).write_text("name: approved workflow\n")
    snapshot, revision = snapshot_checkpoint(
        root, tmp_path / "with-workflow", repository_config.campaign.revision
    )
    values = repository_config.model_dump()
    values["campaign"].update(repository=str(snapshot), revision=revision, scope=["src", ".github"])
    values["task"].update(allowed_paths=["src", path], editable_project_paths=[path])
    config = RepositoryGoalConfig.model_validate(values)
    research = investigation(config, tmp_path / "goal")
    inspected = research.perform(
        SupervisorDecision(
            GoalAction.INSPECT,
            "Read approved workflow",
            "Maintain CI",
            inspection=InspectionRequest(path=path),
        ),
        "inspect-workflow",
        ready_state(config),
    )
    assert "approved workflow" in inspected.excerpt
    assignment = TaskAssignment("Update the approved CI workflow", (path,), (inspected.record_id,))
    prepared = research._task(assignment, replace(ready_state(config), research=(inspected,)))  # pyright: ignore[reportPrivateUsage] - exercise pre-binding scope validator
    workspace = prepare_checkout(snapshot, revision, tmp_path / "candidate", prepared.branch)
    assert writable(path, prepared, workspace)
    assert not writable(".github/unapproved.yml", prepared, workspace)


@pytest.mark.parametrize(
    "path",
    [".env", ".env.production", ".git/config", ".ssh/id_rsa", "credentials/token", "private.key"],
)
def test_secret_files_cannot_be_approved_project_maintenance(
    repository_config: RepositoryGoalConfig, path: str
) -> None:
    values = repository_config.task.model_dump()
    values.update(allowed_paths=[path], editable_project_paths=[path])
    with pytest.raises(ValueError, match="trusted or secret"):
        CodingTaskConfig.model_validate(values)


def test_model_action_protocol_errors_are_distinct_from_provider_transport() -> None:
    def failed(code: str) -> ModelAttemptFailed:
        return ModelAttemptFailed(
            event_id=EventId("e"),
            run_id=RunId("r"),
            occurred_at=datetime.now(UTC),
            sequence=1,
            reason_code=code,
            summary="Recorded adapter failure",
        )

    assert worker_failure([failed("MODEL_INVALID_RESPONSE")]) == FailureKind.PROTOCOL
    assert worker_failure([failed("MODEL_CONNECTION_ERROR")]) == FailureKind.PROVIDER


@pytest.mark.parametrize("worker_started", [False, True])
def test_conflicting_answer_successor_preserves_candidate_answers_and_repeat_identity(
    repository_config: RepositoryGoalConfig, tmp_path: Path, worker_started: bool
) -> None:
    config = repository_config.model_copy(
        update={
            "clarifications": (
                ClarificationPolicy("style", "Which style?", "optional", 10, "Keep style"),
            )
        }
    )
    parent = RepositoryGoalService(tmp_path / "parent")
    child = None
    try:
        state = parent.create(config)
        record_runner(parent.directory, armed=True, status="waiting")
        with pytest.raises(ValueError, match="conflicting answer"):
            prepare_successor(parent.directory)
        assert runner_record(parent.directory)["armed"]  # A refused handoff keeps the runner.
        with parent.harness.store.connect() as db:
            campaign = db.execute("SELECT id FROM campaigns").fetchone()["id"]
        source = (
            parent.directory
            / "worker/coding"
            / ("goal_" + hashlib.sha256(campaign.encode()).hexdigest()[:20])
        )
        if not worker_started:
            source = parent.directory / "handoff-baseline"
        workspace = prepare_checkout(
            Path(config.campaign.repository), config.campaign.revision, source, config.task.branch
        )
        expected = (
            "def parse(value): return int(value)\n"
            if worker_started
            else "def parse(value): return value\n"
        )
        (source / "src/parse.py").write_text(expected)
        candidate = patch_identity(workspace)
        if not worker_started:
            # Remove the test checkout so the production baseline fallback must create it.
            shutil.rmtree(source)
        assert state.policy is not None
        events: tuple[GoalEvent, ...] = (
            QuestionOpened("style", 100),
            ExperimentRecorded("style", "experiment/" + "c" * 20, config.campaign.revision, 100),
            QuestionAnswered("style", "Use the new style", 110),
            ControlAcknowledged("pause"),
        )
        for event in events:
            state = parent.journal.append(GOAL, state.sequence, event)
        parent.close()  # Handoff opens its own service after the paused runner releases ownership.
        first = prepare_successor(parent.directory)
        second = prepare_successor(parent.directory)
        assert first == second
        assert first["started"] is False
        # The successor carries the work; the predecessor is no longer driven.
        assert runner_record(parent.directory) | {"heartbeat_epoch": 0} == {
            "armed": False,
            "status": "stopped",
            "heartbeat_epoch": 0,
            "error": "",
        }
        assert first["parent_candidate_sha256"] == candidate
        child = RepositoryGoalService(Path(str(first["directory"])))
        child_state = child.journal.load(GOAL)
        assert child_state.questions[0].answer == "Use the new style"
        assert not child_state.late_answer_conflict
        assert child.paid_budget().path == parent.directory / "paid-budget.sqlite"
        assert parent.journal.load(GOAL) == state
        assert (source / "src/parse.py").read_text() == expected
    finally:
        if child is not None:
            child.close()
        parent.close()
