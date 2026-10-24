"""Trusted intake, recovery and CLI boundary tests without Docker or network."""

import json
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.domain.coding import CodingEvent
from loopforge.domain.goals import CallReserved, DecisionRecorded, GoalAction, SupervisorDecision
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints import campaign_execution
from loopforge.entrypoints.campaign_models import (
    AcceptanceProbe,
    CampaignConfig,
    CodingCheck,
    CodingTaskConfig,
)
from loopforge.entrypoints.goal_commands import prepared_main as main
from loopforge.entrypoints.supervisor import GOAL, PreparedGoalConfig, PreparedGoalService


@pytest.fixture
def config(tmp_path: Path) -> PreparedGoalConfig:
    workspace = GitWorkspaceManager(tmp_path / "source").materialize(
        FixtureSpec(
            fixture_id="goal-intake",
            files=(FixtureFile(path="src/add.py", content="pass\n"),),
        )
    )

    return PreparedGoalConfig(
        campaign=CampaignConfig(
            repository=str(workspace.root),
            project="brook",
            revision=workspace.base_revision,
            scope=["src"],
        ),
        task=CodingTaskConfig(
            objective="Fix prepared addition behavior",
            kind="bugfix",
            branch="goal/prepared",
            allowed_paths=["src"],
            required_changed_paths=["src/add.py"],
            image="sha256:" + "a" * 64,
            checks=[
                CodingCheck(name="check_project", argv=["/usr/local/bin/python", "-c", "pass"])
            ],
            acceptance_probes=[
                AcceptanceProbe(
                    criterion_id="addition",
                    description="Synthetic addition acceptance",
                    name="check_acceptance_addition",
                    source="assert 2 + 3 == 5",
                )
            ],
            evidence="Prepared fixture",
        ),
        max_cost_microusd=100000,
    )


@pytest.mark.parametrize("nonblocking", [False, True])
def test_specialist_worker_keeps_coordinator_responsive(
    tmp_path: Path,
    config: PreparedGoalConfig,
    monkeypatch: pytest.MonkeyPatch,
    nonblocking: bool,
) -> None:
    config = config.model_copy(
        update={
            "remote_data": "synthetic-fixture",
            "campaign": config.campaign.model_copy(update={"inference_profile": "qwen-native-v1"}),
        }
    )
    service = PreparedGoalService(tmp_path / "nonblocking")
    service.harness.nonblocking_goals = nonblocking
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def held_worker(
        campaign: str,
        task: str,
        campaign_config: CampaignConfig,
        finish: Callable[..., None],
    ) -> None:
        del campaign, campaign_config, finish
        with service.harness.store.connect() as db:
            service.harness.coding.jobs.append(db, task, CodingEvent("start"), "{}")
        entered.set()
        release.wait(5)

    monkeypatch.setattr(service.harness.coding, "drive", held_worker)
    monkeypatch.setattr(campaign_execution, "preflight", lambda _task: None)  # pyright: ignore[reportUnknownLambdaType,reportUnknownArgumentType]
    caller: threading.Thread | None = None
    try:
        service.create(config)
        worker = service.worker()

        def dispatch() -> None:
            try:
                worker.execute(GoalAction.DELEGATE, "one-operation")
            except BaseException as exc:
                errors.append(exc)
            finally:
                returned.set()

        caller = threading.Thread(target=dispatch)
        caller.start()
        assert entered.wait(2), errors
        assert returned.wait(0.1 if not nonblocking else 2) is nonblocking
        if nonblocking:
            assert worker.reconcile().status == "coding"
            with service.harness.store.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM dispatch_requests").fetchone()[0] == 1
    finally:
        release.set()
        if caller is not None:
            caller.join(3)
        service.close()
    assert not errors


def test_cancel_reconciles_unknown_call_and_keeps_spend(
    tmp_path: Path, config: PreparedGoalConfig
) -> None:
    service = PreparedGoalService(tmp_path / "goal")
    try:
        state = service.create(config)
        service.journal.append(
            GOAL, state.sequence, CallReserved("interrupted", 10000, "model configuration")
        )
        state = service.cancel()
        assert state.phase == "cancelled"
        assert state.spent_microusd == 10000
        assert state.reserved_microusd == 0
        assert service.cancel() == state
    finally:
        service.close()


def test_intake_without_trusted_acceptance_denied(
    tmp_path: Path, config: PreparedGoalConfig
) -> None:
    service = PreparedGoalService(tmp_path / "goal")
    try:
        shallow = config.model_copy(
            update={"task": config.task.model_copy(update={"acceptance_probes": []})}
        )
        with pytest.raises(ValueError, match="trusted acceptance"):
            service.create(shallow)
        assert service.journal.load(GOAL).phase == "new"
    finally:
        service.close()


def test_intake_restart_finishes_partial_creation_without_resetting_limits(
    tmp_path: Path, config: PreparedGoalConfig
) -> None:
    directory = tmp_path / "goal"
    service = PreparedGoalService(directory)
    try:
        # Simulate intake saved and campaign created before the GoalCreated event.
        (directory / "intake.json").write_text(config.model_dump_json())
        service.harness.create(config.campaign)
    finally:
        service.close()
    service = PreparedGoalService(directory)
    try:
        state = service.create(config)
        assert state.phase == "ready"
        assert service.create(config) == state
        with service.harness.store.connect() as db:
            assert db.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 1
        changed = config.model_copy(update={"max_goal_seconds": config.max_goal_seconds + 1})
        (directory / "intake.json").write_text(changed.model_dump_json())
        with pytest.raises(ValueError, match="binding"):
            service.worker()
    finally:
        service.close()


def test_cli_create_status_cancel(
    tmp_path: Path,
    config: PreparedGoalConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    directory = tmp_path / "goal"
    monkeypatch.setattr(
        sys, "argv", ["supervisor", "create", "--directory", str(directory), "--config", str(path)]
    )
    main()
    assert json.loads(capsys.readouterr().out)["phase"] == "ready"
    monkeypatch.setattr(sys, "argv", ["supervisor", "status", "--directory", str(directory)])
    main()
    assert json.loads(capsys.readouterr().out)["spent_microusd"] == 0
    monkeypatch.setattr(sys, "argv", ["supervisor", "cancel", "--directory", str(directory)])
    main()
    assert json.loads(capsys.readouterr().out)["phase"] == "cancelled"


def test_prepared_worker_refreshes_guidance_after_supervisor_decision(
    tmp_path: Path, config: PreparedGoalConfig, monkeypatch: pytest.MonkeyPatch
) -> None:

    service = PreparedGoalService(tmp_path / "fresh-guidance")
    try:
        state = service.create(config)
        worker = service.worker()
        state = service.journal.append(GOAL, state.sequence, CallReserved("new-plan", 100))
        service.journal.append(
            GOAL,
            state.sequence,
            DecisionRecorded(
                SupervisorDecision(
                    GoalAction.DELEGATE, "New measured evidence", "Inspect response validation"
                ),
                10,
                "offline",
            ),
        )

        def stop_before_execution(*args: object) -> str:
            message = "offline boundary reached"
            raise ValueError(message)

        monkeypatch.setattr(service.harness.coding, "enqueue", stop_before_execution)
        with pytest.raises(ValueError, match="offline boundary reached"):
            worker.execute(GoalAction.DELEGATE, "new-plan")
        guidance = json.loads(service.harness.coding.assignment_guidance[worker.task])
        assert guidance["latest_supervisor_plan_untrusted"] == "Inspect response validation"
        assert guidance["latest_supervisor_rationale_untrusted"] == "New measured evidence"
    finally:
        service.close()
