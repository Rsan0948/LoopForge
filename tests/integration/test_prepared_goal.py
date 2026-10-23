"""Prepared goal through real Docker worker/checks and fresh review, with restarts."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import loopforge.adapters.gemini_supervisor as gemini_module
import loopforge.entrypoints.goal_review as review_module
from loopforge.adapters.gemini_supervisor import GeminiSupervisor
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.scripted import ScriptedModel
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.goal_review import ReviewRecorded
from loopforge.domain.goals import GoalAction, GoalEvent, GoalState
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunId
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints.campaign_coding import ConfinedCodingTools, local_git_bytes
from loopforge.entrypoints.campaign_models import (
    AcceptanceProbe,
    CampaignConfig,
    CodingCheck,
    CodingTaskConfig,
)
from loopforge.entrypoints.campaign_review import ReviewTools
from loopforge.entrypoints.goal_review import morning_report, review_command
from loopforge.entrypoints.supervisor import GOAL, PreparedGoalConfig, PreparedGoalService


@pytest.fixture(autouse=True)
def fixture_pricing_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini_module, "PRICE_VALID_UNTIL", datetime(2100, 1, 1, tzinfo=UTC))


@pytest.fixture
def config(tmp_path: Path) -> PreparedGoalConfig:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "python:3.12-alpine", "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker image unavailable; no pull or host fallback")
    if result.returncode:
        pytest.skip("Prepared Python container image unavailable; no pull")
    original = GitWorkspaceManager(tmp_path / "source").materialize(
        FixtureSpec(
            fixture_id="phase2-arithmetic",
            files=(
                FixtureFile(path="src/add.py", content="def add(a,b): return 0\n"),
                FixtureFile(path="src/project.test.py", content="assert True\n"),
            ),
        )
    )
    return PreparedGoalConfig(
        campaign=CampaignConfig(
            repository=str(original.root),
            project="brook",
            revision=original.base_revision,
            scope=["src"],
            rounds=1,
            max_iterations=12,
            max_seconds=120,
        ),
        task=CodingTaskConfig(
            objective="Repair integer addition for positive, negative, and zero operands",
            kind="bugfix",
            branch="goal/arithmetic",
            allowed_paths=["src"],
            required_changed_paths=["src/add.py"],
            image=result.stdout.strip(),
            evidence="Synthetic fixture; add(2,3) must equal 5",
            checks=[
                CodingCheck(
                    name="check_project",
                    argv=["/usr/local/bin/python", "-B", "src/project.test.py"],
                )
            ],
            acceptance_probes=[
                AcceptanceProbe(
                    criterion_id="addition",
                    description="Integer addition preserves positive, negative and zero cases",
                    name="check_acceptance_addition",
                    source=(
                        "import sys\nsys.path.insert(0, '/workspace/src')\n"
                        "from add import add\nassert add(2,3)==5\n"
                        "assert add(-4,4)==0\nassert add(0,0)==0\n"
                    ),
                )
            ],
        ),
        max_cost_microusd=500000,
        max_goal_seconds=300,
        remote_data="synthetic-fixture",
    )


def driver(runtime: Runtime, run_id: RunId) -> RunState:
    if isinstance(runtime.tools, ConfinedCodingTools):
        runtime.model = ScriptedModel(
            [
                ActionProposal(
                    ActionId("fix"),
                    "write_file",
                    {"path": "src/add.py", "content": "def add(a,b): return a+b\n"},
                )
            ]
        )
    else:
        assert isinstance(runtime.tools, ReviewTools)
        runtime.model = ScriptedModel(
            [
                *(
                    ActionProposal(ActionId(f"read-{index}"), "read_file", {"path": path})
                    for index, path in enumerate(sorted(runtime.tools.files))
                ),
                ActionProposal(
                    ActionId("report"),
                    "submit_report",
                    {
                        "report": json.dumps(
                            {
                                "summary": "Fresh patch/source review and checks",
                                "findings": [],
                                "unresolved": [],
                            }
                        )
                    },
                ),
            ]
        )
    return runtime.resume(run_id)


def gemini_fixture(request: httpx.Request) -> httpx.Response:
    context = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"])
    actions = context["allowed_actions"]
    action = next(
        action
        for action in ("delegate", "verify", "retry", "deliver", "block")
        if action in actions
    )
    return httpx.Response(
        200,
        json={
            "modelVersion": "offline-argon-fixture",
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 30,
                "thoughtsTokenCount": 20,
                "totalTokenCount": 150,
            },
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "action": action,
                                        "rationale": "Fixture evidence supports this action",
                                        "plan": "Delegate, verify independently, deliver candidate",
                                    }
                                ),
                                "thoughtSignature": "fixture-metadata",
                            }
                        ]
                    },
                }
            ],
        },
    )


def test_goal_delivers_candidate_after_lost_ack_and_service_restart(  # noqa: PLR0915 - complete restart and acceptance recovery
    tmp_path: Path, config: PreparedGoalConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "goal"
    with httpx.Client(transport=httpx.MockTransport(gemini_fixture)) as client:
        supervisor = GeminiSupervisor("fixture-key", client=client)
        service = PreparedGoalService(directory)
        try:
            created = service.create(config)
            assert service.create(config) == created
            service.harness.coding.driver = driver
            coordinator = service.coordinator(supervisor)
            pending = coordinator.step(GOAL)
            assert pending.phase == "action"
            # Worker committed its result; coordinator acknowledgement is lost.
            service.worker().execute(GoalAction.DELEGATE, pending.call_key)
        finally:
            service.close()
        resumed = PreparedGoalService(directory)
        try:
            resumed.harness.coding.driver = driver
            coordinator = resumed.coordinator(supervisor)
            assert coordinator.step(GOAL).observation.status == "review_pending"
            assert coordinator.step(GOAL).phase == "action"
            assert coordinator.step(GOAL).observation.status == "patch_ready"
            assert coordinator.step(GOAL).phase == "action"
            result = coordinator.step(GOAL)
            assert result.phase == "delivered"
            assert result.spent_microusd == 2100
            assert result.plan == "Delegate, verify independently, deliver candidate"
            worker = resumed.worker()
            with resumed.harness.store.connect() as db:
                state = resumed.harness.coding.jobs.state(db, worker.task)
                assert state.status == "patch_ready"  # delivery grants no local acceptance
                assert state.attempts == state.review_attempts == 1
            assert (
                Path(config.campaign.repository) / "src/add.py"
            ).read_text() == "def add(a,b): return 0\n"
            candidate = directory / "worker/coding" / worker.task / "src/add.py"
            assert candidate.read_text() == "def add(a,b): return a+b\n"
            assert coordinator.step(GOAL) == result
        finally:
            resumed.close()
        report = morning_report(directory)
        assert report["diff"]
        assert not report["candidate_changed_since_verification"]
        original_reduce = review_module.reduce_goal

        def interrupted_ack(state: GoalState, event: GoalEvent) -> GoalState:
            if isinstance(event, ReviewRecorded) and event.commit != "0" * 40:
                msg = "Simulated crash after worker acceptance before goal acknowledgement"
                raise RuntimeError(msg)
            return original_reduce(state, event)

        monkeypatch.setattr(review_module, "reduce_goal", interrupted_ack)
        with pytest.raises(RuntimeError, match="Simulated crash"):
            review_command(directory, "accept")
        monkeypatch.setattr(review_module, "reduce_goal", original_reduce)
        candidate_root = candidate.parents[1]
        accepted_commit = local_git_bytes(candidate_root, "rev-parse", "HEAD").decode().strip()
        candidate.write_text("def add(a,b): return a+b  # Later operator inspection\n")
        local_git_bytes(
            candidate_root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@localhost",
            "commit",
            "-am",
            "Later local inspection",
        )
        assert (
            local_git_bytes(candidate_root, "rev-parse", "HEAD").decode().strip() != accepted_commit
        )
        accepted = review_command(directory, "accept")
        assert accepted["accepted_commit"] == accepted_commit
        assert morning_report(directory)["candidate_changed_since_verification"] is True
        assert accepted["disposition"] == "accepted"
        assert review_command(directory, "accept") == accepted
        assert (
            Path(config.campaign.repository) / "src/add.py"
        ).read_text() == "def add(a,b): return 0\n"


def test_intake_authority_and_service_ownership_are_immutable(
    tmp_path: Path, config: PreparedGoalConfig
) -> None:
    directory = tmp_path / "goal"
    service = PreparedGoalService(directory)
    try:
        original = service.create(config)
        with pytest.raises(ValueError, match="another campaign"):
            PreparedGoalService(directory)
        expanded = config.model_copy(update={"remote_data": "prepared-repository"})
        with pytest.raises(ValueError, match="immutable"):
            service.create(expanded)
        (directory / "intake.json").write_text(expanded.model_dump_json())
        with pytest.raises(ValueError, match="binding"):
            service.worker()
        (directory / "intake.json").write_text(config.model_dump_json())
        assert service.journal.load(GOAL) == original
        cancelled = service.cancel()
        assert cancelled.phase == "cancelled"
        assert service.cancel() == cancelled
    finally:
        service.close()


def test_goal_deadline_blocks_worker_without_dispatch(
    tmp_path: Path, config: PreparedGoalConfig
) -> None:
    service = PreparedGoalService(tmp_path / "goal")
    try:
        service.create(config)
        worker = service.worker()
        service.harness.coding.execution_deadline_epoch = 1
        service.harness.coding.driver = driver
        with pytest.raises(ValueError, match="before starting"):
            worker.execute(GoalAction.DELEGATE, "deadline-test")
        with service.harness.store.connect() as db:
            assert service.harness.coding.jobs.state(db, worker.task).attempts == 0
    finally:
        service.close()


def test_retry_survives_crash_after_requeue_before_dispatch(
    tmp_path: Path, config: PreparedGoalConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "goal"
    service = PreparedGoalService(directory)
    try:
        service.create(config)
        service.harness.coding.driver = lambda runtime, run_id: runtime.cancel(
            run_id, summary="Injected first-attempt failure"
        )
        worker = service.worker()
        assert worker.execute(GoalAction.DELEGATE, "first-attempt").status == "attention"

        def crash(*_args: object, **_kwargs: object) -> dict[str, object]:
            message = "Injected crash after durable retry"
            raise SystemExit(message)

        monkeypatch.setattr(service.harness, "tick", crash)
        with pytest.raises(SystemExit, match="durable retry"):
            worker.execute(GoalAction.RETRY, "retry-after-failure")
    finally:
        service.close()
    restarted = PreparedGoalService(directory)
    try:
        restarted.harness.coding.driver = driver
        worker = restarted.worker()
        assert worker.execute(GoalAction.RETRY, "retry-after-failure").status == "review_pending"
        # Same intent cannot create another successor after its result is durable.
        assert worker.execute(GoalAction.RETRY, "retry-after-failure").status == "review_pending"
        with restarted.harness.store.connect() as db:
            assert restarted.harness.coding.jobs.state(db, worker.task).attempts == 2
        with pytest.raises(ValueError, match="different intent"):
            worker.execute(GoalAction.VERIFY, "retry-after-failure")
    finally:
        restarted.close()


def test_relative_goal_directory_materializes_correct_candidate(
    tmp_path: Path, config: PreparedGoalConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    service = PreparedGoalService(Path("relative-goal"))
    try:
        service.create(config)
        service.harness.coding.driver = driver
        worker = service.worker()
        assert worker.execute(GoalAction.DELEGATE, "relative-path").status == "review_pending"
        assert (tmp_path / "relative-goal/worker/coding" / worker.task / "src/add.py").is_file()
    finally:
        service.close()
