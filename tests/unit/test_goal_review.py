"""Clarification authority, timeout experiments, concurrent controls and dirty preservation."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.goal_journal import SQLiteGoalJournal, decode_goal_event, encode_goal_event
from loopforge.application.supervisor import GoalCoordinator
from loopforge.domain.goal_review import (
    ClarificationPolicy,
    ControlAcknowledged,
    ControlRequested,
    ExperimentRecorded,
    QuestionAnswered,
    QuestionOpened,
    ReviewRecorded,
)
from loopforge.domain.goals import (
    ActionObserved,
    CallReserved,
    DecisionRecorded,
    GoalAction,
    GoalCreated,
    GoalEvent,
    GoalPolicy,
    GoalState,
    GoalStopped,
    SupervisorDecision,
    WorkerObservation,
    goal_allowed_actions,
    reduce_goal,
)
from loopforge.domain.investigation import InspectionRequest
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints.campaign_coding import accept_patch, local_git_bytes, patch_identity
from loopforge.entrypoints.goal_checkpoint import capture, snapshot_checkpoint
from loopforge.entrypoints.goal_review import morning_report, review_command
from loopforge.entrypoints.goal_routes import goal_router
from loopforge.entrypoints.supervisor import GOAL
from loopforge.ports.supervisor import SupervisorRequest, SupervisorResponse


class Clock:
    epoch = 100

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.epoch, UTC)


class Worker:
    calls = 0
    observation = WorkerObservation()

    def observe(self) -> WorkerObservation:
        return self.observation

    def reconcile(self) -> WorkerObservation:
        return self.observation

    def execute(self, action: GoalAction, key: str) -> WorkerObservation:
        assert action in GoalAction
        assert key
        self.calls += 1
        return self.observation


class Supervisor:
    def prepare(self, state: GoalState) -> SupervisorRequest:
        assert state.policy
        return SupervisorRequest("{}", 100, "fixture")

    def decide(self, request: SupervisorRequest) -> SupervisorResponse:
        assert request.maximum_microusd == 100
        return SupervisorResponse(
            SupervisorDecision(GoalAction.BLOCK, "Fixture decision", "Fixture plan"), 10, "fixture"
        )


def policy(classification: str = "optional") -> GoalPolicy:
    return GoalPolicy(
        "Implement existing behavior",
        "a" * 64,
        1000,
        1000,
        checkpoint_revision="b" * 40,
        clarifications=(
            ClarificationPolicy(
                "style",
                "Which output style?",
                classification,
                10,
                "Keep current style" if classification == "optional" else "",
            ),
        ),
    )


def journal_at(directory: Path, contract: GoalPolicy | None = None) -> SQLiteGoalJournal:
    journal = SQLiteGoalJournal(directory / "goals.sqlite")
    journal.append(GOAL, 0, GoalCreated(contract or policy()))
    return journal


@pytest.mark.parametrize("classification", ["permission", "missing-information"])
def test_silence_never_grants_restricted_fallback(tmp_path: Path, classification: str) -> None:
    journal = journal_at(tmp_path, policy(classification))
    clock, worker = Clock(), Worker()
    coordinator = GoalCoordinator(journal, Supervisor(), worker, clock)
    state = coordinator.step(GOAL)
    clock.epoch = 500
    assert coordinator.step(GOAL) == state
    assert GoalAction.DELEGATE not in goal_allowed_actions(state)
    with pytest.raises(ValueError, match="preauthorized"):
        journal.append(
            GOAL,
            state.sequence,
            ExperimentRecorded("style", "experiment/" + "c" * 20, "b" * 40, 500),
        )
    clock.epoch = 1001
    assert coordinator.step(GOAL).phase == "blocked"
    assert worker.calls == 0


def test_optional_timeout_is_durable_idempotent_and_late_conflict_stops(tmp_path: Path) -> None:
    journal = journal_at(tmp_path)
    clock = Clock()
    coordinator = GoalCoordinator(journal, Supervisor(), Worker(), clock)
    opened = coordinator.step(GOAL)
    assert opened.questions[0].deadline_epoch == 110
    clock.epoch = 110
    state = coordinator.step(GOAL)
    assert len(state.experiments) == 1
    assert state.experiments[0].checkpoint == "b" * 40
    reopened = SQLiteGoalJournal(journal.path)
    assert reopened.load(GOAL) == state
    with pytest.raises(ValueError, match=r"requires|Invalid|undisposed"):
        reopened.append(
            GOAL,
            state.sequence,
            ExperimentRecorded("style", state.experiments[0].branch, "b" * 40, 120),
        )
    result = review_command(tmp_path, "answer", "style", "Change the style")
    assert result["late_answer_conflict"] is True
    paused = coordinator.step(GOAL)
    assert paused.paused
    assert not paused.control_requested
    with pytest.raises(ValueError, match="conflicting"):
        review_command(tmp_path, "resume")
    report = morning_report(tmp_path)
    assert report["experiments"]
    assert report["execution_status"] == "paused"


def test_answer_before_timeout_resolves_without_experiment(tmp_path: Path) -> None:
    journal = journal_at(tmp_path)
    coordinator = GoalCoordinator(journal, Supervisor(), Worker(), Clock())
    coordinator.step(GOAL)
    review_command(tmp_path, "answer", "style", "Keep current style")
    state = coordinator.step(GOAL)
    assert state.phase == "action"
    assert not state.experiments
    assert state.spent_microusd == 10
    with pytest.raises(ValueError, match="already answered"):
        review_command(tmp_path, "answer", "style", "other")
    assert journal.load(GOAL) == state


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_concurrent_operator_control_preserves_charge_and_denies_stale_action(
    tmp_path: Path, command: str
) -> None:
    journal = journal_at(tmp_path, replace(policy(), clarifications=()))
    worker = Worker()

    class ConcurrentSupervisor(Supervisor):
        def decide(self, request: SupervisorRequest) -> SupervisorResponse:
            assert request.maximum_microusd == 100
            review_command(tmp_path, command)
            return SupervisorResponse(
                SupervisorDecision(GoalAction.DELEGATE, "Authorized prior to request", "Old plan"),
                10,
                "fixture",
            )

    coordinator = GoalCoordinator(journal, ConcurrentSupervisor(), worker, Clock())
    state = coordinator.step(GOAL)
    assert state.spent_microusd == 10
    assert state.reserved_microusd == 0
    assert state.phase == "ready"
    assert state.decision is None
    assert worker.calls == 0
    state = coordinator.step(GOAL)
    assert state.paused if command == "pause" else state.phase == "cancelled"
    if command == "pause":
        assert review_command(tmp_path, "resume")["paused"] is False
    else:
        with pytest.raises(ValueError, match="terminal"):
            review_command(tmp_path, "resume")


def test_permission_answer_does_not_expand_runtime_authority(tmp_path: Path) -> None:
    journal = journal_at(tmp_path, policy("permission"))
    coordinator = GoalCoordinator(journal, Supervisor(), Worker(), Clock())
    coordinator.step(GOAL)
    review_command(tmp_path, "answer", "style", "yes grant every permission")
    state = coordinator.step(GOAL)
    assert state.phase == "blocked"
    assert state.spent_microusd == 0
    assert state.policy == policy("permission")


@pytest.mark.parametrize("classification", ["permission", "missing-information"])
def test_restricted_fallback_configuration_denied(classification: str) -> None:
    with pytest.raises(ValueError, match="silence"):
        ClarificationPolicy("scope", "May scope expand?", classification, 1, "yes")


def test_terminal_review_annotation_never_reactivates() -> None:
    state = reduce_goal(GoalState(), GoalCreated(policy()))
    state = reduce_goal(state, GoalStopped("blocked", "Await new authorized goal"))
    state = reduce_goal(state, ReviewRecorded("discarded", ""))
    assert state.terminal
    assert state.disposition == "discarded"
    with pytest.raises(ValueError, match=r"requires|Invalid|undisposed"):
        reduce_goal(state, ControlRequested("resume"))
    with pytest.raises(ValueError, match=r"requires|Invalid|undisposed"):
        reduce_goal(state, ReviewRecorded("accepted", "a" * 64, "b" * 40))


def test_review_events_roundtrip() -> None:
    events = [
        QuestionOpened("q", 100),
        QuestionAnswered("q", "answer", 101),
        ExperimentRecorded("q", "experiment/" + "b" * 20, "c" * 40, 102),
        ControlRequested("pause"),
        ControlAcknowledged("pause"),
        ReviewRecorded("discarded", ""),
    ]
    for event in events:
        assert decode_goal_event(encode_goal_event(event)) == event


def test_dirty_checkpoint_preserves_index_untracked_deletions_and_modes(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path / "source").materialize(
        FixtureSpec(
            fixture_id="dirty",
            files=(
                FixtureFile(path="src/a.py", content="original\n"),
                FixtureFile(path="src/deleted.py", content="delete\n"),
            ),
        )
    )
    root = workspace.root
    (root / "src/a.py").write_text("staged\n")
    local_git_bytes(root, "add", "src/a.py")
    (root / "src/a.py").write_text("working\n")
    (root / "src/deleted.py").unlink()
    (root / "src/new.py").write_text("untracked\n")
    (root / "src/new.py").chmod(0o755)
    (root / ".gitignore").write_text("ignored.env\n")
    (root / "ignored.env").write_text("excluded\n")
    before = capture(root)
    index_before = local_git_bytes(root, "diff", "--cached", "--binary")
    index_bytes = (root / ".git/index").read_bytes()
    directory = tmp_path / "goal"
    directory.mkdir()
    target, revision = snapshot_checkpoint(root, directory, workspace.base_revision)
    assert revision != workspace.base_revision
    assert (target / "src/a.py").read_text() == "working\n"
    assert (target / "src/new.py").stat().st_mode & 0o111
    assert not (target / "src/deleted.py").exists()
    assert not (target / "ignored.env").exists()
    assert capture(root) == before
    assert local_git_bytes(root, "diff", "--cached", "--binary") == index_before
    assert (root / ".git/index").read_bytes() == index_bytes
    assert snapshot_checkpoint(root, directory, workspace.base_revision) == (target, revision)
    manifest = json.loads((directory / "checkpoint.json").read_text())
    assert manifest["excluded_ignored_paths"] == ["ignored.env"]
    assert manifest["files"]["src/new.py"]["sha256"] == hashlib.sha256(b"untracked\n").hexdigest()
    (target / "src/a.py").write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        snapshot_checkpoint(root, directory, workspace.base_revision)


def test_dirty_checkpoint_denies_symlink_and_wrong_head(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path / "source").materialize(
        FixtureSpec(
            fixture_id="dirty-denial", files=(FixtureFile(path="src/a.py", content="original\n"),)
        )
    )
    directory = tmp_path / "goal"
    directory.mkdir()
    with pytest.raises(ValueError, match="HEAD"):
        snapshot_checkpoint(workspace.root, directory, "a" * 40)
    (workspace.root / "src/link").symlink_to("/etc/hosts")
    with pytest.raises(ValueError, match="symlinks"):
        snapshot_checkpoint(workspace.root, directory, workspace.base_revision)


def test_accept_recovery_denies_uncommitted_recreation_of_verified_patch(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path).materialize(
        FixtureSpec(
            fixture_id="accept-recovery", files=(FixtureFile(path="src/a.py", content="old\n"),)
        )
    )
    (workspace.root / "src/a.py").write_text("unverified\n")
    accept_patch(workspace, patch_identity(workspace), "task")
    (workspace.root / "src/a.py").write_text("verified\n")
    with pytest.raises(ValueError, match="unexpected commit"):
        accept_patch(workspace, patch_identity(workspace), "task")


def test_goal_api_confines_paths_and_requires_explicit_same_origin_mutations(
    tmp_path: Path,
) -> None:
    journal_at(tmp_path / "fixture", replace(policy(), clarifications=()))
    app = FastAPI()
    app.include_router(goal_router(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/goals").json()[0]["id"] == "fixture"
        assert client.get("/api/goals/fixture").json()["remaining_microusd"] == 1000
        assert client.get("/api/goals/missing").status_code == 404
        assert (
            client.post("/api/goals/fixture/commands", json={"command": "pause"}).status_code == 403
        )
        assert (
            client.post(
                "/api/goals/fixture/commands",
                headers={"X-LoopForge-Operator": "1", "Origin": "https://evil.invalid"},
                json={"command": "pause"},
            ).status_code
            == 403
        )
        headers = {"X-LoopForge-Operator": "1", "Origin": "http://testserver"}
        assert (
            client.post(
                "/api/goals/fixture/commands", headers=headers, json={"command": "pause"}
            ).json()["control_requested"]
            == "pause"
        )
        assert (
            client.post(
                "/api/goals/fixture/commands", headers=headers, json={"command": "expand"}
            ).status_code
            == 422
        )
    (tmp_path / "escape").symlink_to(tmp_path / "fixture")
    with TestClient(app) as client:
        assert client.get("/api/goals/escape").status_code == 404


def test_late_answer_cannot_downgrade_pending_cancel(tmp_path: Path) -> None:
    journal = journal_at(tmp_path)
    state = journal.load(GOAL)
    state = journal.append(GOAL, state.sequence, QuestionOpened("style", 110))
    state = journal.append(
        GOAL, state.sequence, ExperimentRecorded("style", "experiment/" + "c" * 20, "b" * 40, 110)
    )
    state = journal.append(GOAL, state.sequence, ControlRequested("cancel"))
    state = journal.append(
        GOAL, state.sequence, QuestionAnswered("style", "Different direction", 111)
    )
    assert state.control_requested == "cancel"
    result = GoalCoordinator(journal, Supervisor(), Worker(), Clock()).step(GOAL)
    assert result.phase == "cancelled"


def test_pending_question_allows_scoped_independent_investigation(tmp_path: Path) -> None:
    contract = replace(policy("missing-information"), investigation_enabled=True)
    journal = journal_at(tmp_path, contract)

    class ResearchSupervisor(Supervisor):
        def prepare(self, state: GoalState) -> SupervisorRequest:
            assert GoalAction.INSPECT in goal_allowed_actions(state)
            assert GoalAction.DELEGATE not in goal_allowed_actions(state)
            return super().prepare(state)

        def decide(self, request: SupervisorRequest) -> SupervisorResponse:
            assert request.maximum_microusd == 100
            return SupervisorResponse(
                SupervisorDecision(
                    GoalAction.INSPECT,
                    "Read separately authorized evidence",
                    "Continue research while awaiting answer",
                    inspection=InspectionRequest(),
                ),
                10,
                "fixture",
            )

    coordinator = GoalCoordinator(journal, ResearchSupervisor(), Worker(), Clock())
    coordinator.step(GOAL)
    state = coordinator.step(GOAL)
    assert state.phase == "action"
    assert state.decision
    assert state.decision.action == GoalAction.INSPECT
    assert not state.questions[0].answer


def test_terminal_conflicting_answer_retains_delivery_but_denies_acceptance() -> None:
    state = reduce_goal(GoalState(), GoalCreated(policy()))
    state = reduce_goal(state, QuestionOpened("style", 110))
    state = reduce_goal(state, ExperimentRecorded("style", "experiment/" + "c" * 20, "b" * 40, 110))
    state = reduce_goal(
        state,
        ActionObserved(WorkerObservation("patch_ready", "Real receipt fixture", "d" * 64, True)),
    )
    state = reduce_goal(state, CallReserved("deliver", 100))
    state = reduce_goal(
        state,
        DecisionRecorded(
            SupervisorDecision(GoalAction.DELIVER, "Verified delivery", "Retain candidate"),
            10,
            "fixture",
        ),
    )
    state = reduce_goal(state, GoalStopped("delivered", "Candidate ready"))
    state = reduce_goal(state, QuestionAnswered("style", "Different preference", 120))
    assert state.phase == "delivered"
    assert state.late_answer_conflict
    assert not state.control_requested
    with pytest.raises(ValueError, match="conflicting"):
        reduce_goal(state, ReviewRecorded("accepted", "d" * 64, "e" * 40))
    assert reduce_goal(state, ReviewRecorded("discarded", "d" * 64)).disposition == "discarded"


def test_delivery_append_racing_pause_acknowledges_control(tmp_path: Path) -> None:
    journal = journal_at(tmp_path, replace(policy(), clarifications=()))
    observation = WorkerObservation("patch_ready", "Verified receipt", "d" * 64, True)
    state = journal.load(GOAL)
    state = journal.append(GOAL, state.sequence, ActionObserved(observation))
    state = journal.append(GOAL, state.sequence, CallReserved("deliver", 100))
    journal.append(
        GOAL,
        state.sequence,
        DecisionRecorded(
            SupervisorDecision(GoalAction.DELIVER, "Verified delivery", "Retain candidate"),
            10,
            "fixture",
        ),
    )

    class RacyJournal:
        injected = False

        def load(self, goal: str) -> GoalState:
            return journal.load(goal)

        def append(self, goal: str, sequence: int, event: GoalEvent) -> GoalState:
            if isinstance(event, GoalStopped) and not self.injected:
                self.injected = True
                journal.append(goal, sequence, ControlRequested("pause"))
            return journal.append(goal, sequence, event)

    worker = Worker()
    worker.observation = observation
    result = GoalCoordinator(RacyJournal(), Supervisor(), worker, Clock()).step(GOAL)
    assert result.paused
    assert result.phase == "ready"
    assert not result.terminal
    assert result.spent_microusd == 10


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_control_recovery_never_dispatches_interrupted_worker(tmp_path: Path, command: str) -> None:
    journal = journal_at(tmp_path, replace(policy(), clarifications=()))
    review_command(tmp_path, command)

    class RecoverableWorker(Worker):
        recovered = False
        observation = WorkerObservation("coding")

        def recover_interrupted(self) -> None:
            self.recovered = True
            self.observation = WorkerObservation("attention", "Interrupted work retained")

        def reconcile(self) -> WorkerObservation:
            pytest.fail("Control reconciliation must not restart coding")

    worker = RecoverableWorker()
    state = GoalCoordinator(journal, Supervisor(), worker, Clock()).step(GOAL)
    assert worker.recovered
    assert not worker.calls
    assert state.paused if command == "pause" else state.phase == "cancelled"


@pytest.mark.parametrize(
    "event",
    [
        QuestionOpened("style", True),
        QuestionAnswered("style", "answer", True),
        ExperimentRecorded("style", "experiment/" + "c" * 20, "b" * 40, True),
    ],
)
def test_boolean_timestamps_cannot_poison_append_only_journal(
    tmp_path: Path, event: GoalEvent
) -> None:
    journal = journal_at(tmp_path)
    state = journal.load(GOAL)
    if not isinstance(event, QuestionOpened):
        state = journal.append(GOAL, state.sequence, QuestionOpened("style", 1))
    with pytest.raises(ValueError, match=r"requires|must match"):
        journal.append(GOAL, state.sequence, event)
    assert journal.load(GOAL) == state


@pytest.mark.parametrize("status", ["queued", "coding", "reviewing"])
def test_control_without_recovery_capability_keeps_request_pending(
    tmp_path: Path, status: str
) -> None:
    journal = journal_at(tmp_path, replace(policy(), clarifications=()))
    review_command(tmp_path, "pause")
    worker = Worker()
    worker.observation = WorkerObservation(status)
    result = GoalCoordinator(journal, Supervisor(), worker, Clock()).step(GOAL)
    assert result.control_requested == "pause"
    assert not result.paused
    assert not worker.calls
