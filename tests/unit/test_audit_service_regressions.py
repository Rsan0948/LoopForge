"""Offline regression checks for service scheduling and truthful qualification."""

import hashlib
import itertools
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import FunctionType, SimpleNamespace
from typing import Literal, cast

import pytest

from loopforge.adapters.git_workspace import GitWorkspace, GitWorkspaceManager
from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.domain.goal_review import (
    ClarificationPolicy,
    ControlAcknowledged,
    ControlRequested,
    QuestionAnswered,
    QuestionOpened,
)
from loopforge.domain.goals import AssignmentVersion, GoalCreated, GoalEvent, GoalPolicy, GoalState
from loopforge.domain.investigation import TaskAssignment
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints import autonomy_qualification as qualification
from loopforge.entrypoints import goal_bootstrap, goal_review, goal_runner
from loopforge.entrypoints.campaign_models import (
    AcceptanceProbe,
    CampaignConfig,
    CodingCheck,
    CodingTaskConfig,
)
from loopforge.entrypoints.repository_goal_models import ApprovedEnvironment, RepositoryGoalConfig
from loopforge.entrypoints.supervisor import GOAL


def no_action(_value: object = None) -> None:
    pass


def offline_provider(_key: str, **_wiring: object) -> SimpleNamespace:
    # Stands in for an attached provider; the bootstrap requires the evidence store.
    return SimpleNamespace(close=no_action, evidence_store="offline-evidence")


def offline_key(_path: Path | None) -> str:
    return "unused-offline-fixture"


class MemoryJournal:
    def __init__(self, state: GoalState) -> None:
        self.state = state

    def load(self, goal: str) -> GoalState:
        assert goal == GOAL
        return self.state


def expired_step(_goal: str) -> GoalState:
    pytest.fail("Expired trial must not request another decision")


def intake(tmp_path: Path, *, extra_file: bool = False) -> RepositoryGoalConfig:
    files = [FixtureFile(path="src/main.py", content="value = 1\n")]
    if extra_file:
        files.append(FixtureFile(path="src/second.py", content="value = 2\n"))
    workspace = GitWorkspaceManager(tmp_path).materialize(
        FixtureSpec(fixture_id="second" if extra_file else "first", files=tuple(files))
    )
    check = CodingCheck(name="check_project", argv=["/usr/bin/true"])
    image = "sha256:" + "a" * 64
    return RepositoryGoalConfig(
        campaign=CampaignConfig(
            repository=str(workspace.root),
            project="brook",
            revision=workspace.base_revision,
            scope=["src"],
        ),
        task=CodingTaskConfig(
            objective="Implement and independently verify the bounded requested behavior",
            kind="bugfix",
            branch="audit/feature",
            allowed_paths=["src"],
            image=image,
            checks=[check],
            acceptance_probes=[
                AcceptanceProbe(
                    criterion_id=name,
                    name="check_acceptance_" + name,
                    description="Independent behavior for " + name,
                    source="assert True",
                )
                for name in ["first", "second"]
            ],
            evidence="Explicit test-owned fixture authority",
        ),
        environments=[
            ApprovedEnvironment(
                environment_id="stdlib",
                description="Offline fixture environment",
                image=image,
                readiness=[check],
                checks=[CodingCheck(name="check_other", argv=["/usr/bin/true"])],
            )
        ],
        max_cost_microusd=1000,
        max_goal_seconds=30,
    )


@pytest.fixture
def plan(tmp_path: Path) -> qualification.QualificationPlan:
    first, second = intake(tmp_path), intake(tmp_path, extra_file=True)
    cases: list[qualification.QualificationCase] = []
    for scenario, kind, config in [
        ("wrong-hypothesis", "bugfix", first),
        ("healthy-feature", "feature", first),
        ("scaffold-creation", "launch", second),
        ("multistage", "feature", second),
    ]:
        data = config.model_dump()
        data["task"]["kind"] = kind
        cases.append(
            qualification.QualificationCase(
                name=scenario,
                scenario=cast(
                    Literal[
                        "wrong-hypothesis", "healthy-feature", "scaffold-creation", "multistage"
                    ],
                    scenario,
                ),
                intake=RepositoryGoalConfig.model_validate(data),
                wrong_hypothesis_path="src/main.py" if scenario == "wrong-hypothesis" else None,
                milestone_criteria=["first", "second"] if scenario == "multistage" else [],
            )
        )
    return qualification.QualificationPlan(cases=cases, paid_cap_microusd=8000)


@pytest.mark.parametrize("paused", [True, False], ids=["paused", "waiting-for-answer"])
def test_service_yields_idle_goal_and_runs_next_armed_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paused: bool
) -> None:
    second_seen = threading.Event()
    calls: list[str] = []

    class Service:
        def __init__(self, directory: Path) -> None:
            self.directory = directory
            self.harness = SimpleNamespace()
            state = GoalState(
                phase="delivered" if directory.name == "b-ready" else "ready", paused=paused
            )
            self.journal = MemoryJournal(state)
            calls.append(directory.name)
            if directory.name == "b-ready":
                second_seen.set()

        def worker(self):
            return SimpleNamespace(recover_interrupted=lambda: None)

        def coordinator(self, _supervisor: object) -> SimpleNamespace:
            return SimpleNamespace(step=self.journal.load)

        def close(self) -> None:
            pass

    monkeypatch.setattr(goal_bootstrap, "PreparedGoalService", Service)
    monkeypatch.setattr(goal_bootstrap, "GeminiSupervisor", offline_provider)
    monkeypatch.setattr(goal_bootstrap, "load_gemini_key", offline_key)
    for name in ["a-idle", "b-ready"]:
        directory = tmp_path / name
        directory.mkdir()
        goal_runner.record_runner(directory, armed=True, status="queued")
    service = goal_runner.GoalExecutionService(tmp_path, check=lambda _: None)
    try:
        service.start()
        assert second_seen.wait(2), "Paused/waiting work monopolized the execution service"
        assert calls[:2] == ["a-idle", "b-ready"]
        assert goal_runner.runner_record(tmp_path / "a-idle")["status"] == "waiting"
    finally:
        service.close()


def test_morning_report_selects_current_version_and_detects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coding = tmp_path / "worker" / "coding"
    campaign = "campaign_fixture"
    manager = GitWorkspaceManager(coding)
    versions: list[GitWorkspace] = []
    for version in [1, 2]:
        seed = campaign if version == 1 else f"{campaign}:milestone:{version}"
        task = "goal_" + hashlib.sha256(seed.encode()).hexdigest()[:20]
        versions.append(
            manager.materialize(
                FixtureSpec(
                    fixture_id=task, files=(FixtureFile(path="src/main.py", content="value = 1\n"),)
                )
            )
        )
    current = versions[-1]
    (current.root / "src/main.py").write_text("value = 2\n")
    with sqlite3.connect(tmp_path / "worker" / "campaigns.sqlite") as db:
        db.execute("CREATE TABLE campaigns(id TEXT)")
        db.execute("INSERT INTO campaigns VALUES(?)", (campaign,))
    assignment = TaskAssignment(
        "Implement the next verified stage", ("src/main.py",), ("e_source",)
    )
    state = GoalState(
        policy=GoalPolicy(
            "Versioned feature", "a" * 64, 1000, 1000, checkpoint_revision=current.base_revision
        ),
        phase="ready",
        assignment_versions=(
            AssignmentVersion(assignment, "stdlib"),
            AssignmentVersion(assignment, "stdlib"),
        ),
    )

    def load_state(_journal: SQLiteGoalJournal, goal: str) -> GoalState:
        assert goal == GOAL
        return state

    monkeypatch.setattr(SQLiteGoalJournal, "load", load_state)
    report = goal_review.morning_report(tmp_path)
    assert report["candidate_directory"] == str(current.root)
    diff = report["diff"]
    assert isinstance(diff, str)
    assert "+value = 2" in diff
    state = replace(
        state,
        observation=replace(
            state.observation, candidate_sha256=cast("str", report["current_candidate_sha256"])
        ),
    )
    assert not goal_review.morning_report(tmp_path)["candidate_changed_since_verification"]
    (current.root / "src/main.py").write_text("value = 3\n")
    assert goal_review.morning_report(tmp_path)["candidate_changed_since_verification"]


def test_qualification_rejects_cosmetic_scenarios_and_one_repository_shape(
    plan: qualification.QualificationPlan,
) -> None:
    data = plan.model_dump()
    for case in data["cases"]:
        case["intake"] = data["cases"][0]["intake"]
    with pytest.raises(ValueError, match=r"workload|creation"):
        qualification.QualificationPlan.model_validate(data)
    data = plan.model_dump()
    for case in data["cases"]:
        case["intake"]["campaign"] = data["cases"][0]["intake"]["campaign"]
    with pytest.raises(ValueError, match="repository shape"):
        qualification.QualificationPlan.model_validate(data)


@pytest.mark.parametrize(
    "change",
    [
        {"scenario_evidence": False},
        {"interventions": 1},
        {"protocol_rejections": 1},
        {"complete": False},
    ],
)
def test_qualification_threshold_checks_real_outcome_evidence(
    plan: qualification.QualificationPlan, change: dict[str, object]
) -> None:
    rows: list[dict[str, object]] = [
        {
            "case": case.name,
            "repeat": repeat,
            "complete": True,
            "scenario_evidence": True,
            "interventions": 0,
            "protocol_rejections": 0,
        }
        for case in plan.cases
        for repeat in [1, 2]
    ]
    assert qualification.qualification_passed(plan, rows)
    rows[0].update(change)
    assert not qualification.qualification_passed(plan, rows)


def test_qualification_requires_complete_distinct_repetitions(
    plan: qualification.QualificationPlan,
) -> None:
    rows: list[dict[str, object]] = [
        {
            "case": case.name,
            "repeat": repeat,
            "complete": True,
            "scenario_evidence": True,
            "interventions": 0,
            "protocol_rejections": 0,
        }
        for case in plan.cases
        for repeat in [1, 2]
    ]
    assert not qualification.qualification_passed(plan, rows[:-1])
    rows[1]["repeat"] = 1
    assert not qualification.qualification_passed(plan, rows)


def test_trial_metrics_count_actual_human_events_and_rejections(
    tmp_path: Path, plan: qualification.QualificationPlan
) -> None:
    journal = SQLiteGoalJournal(tmp_path / "goals.sqlite")
    events: list[GoalEvent] = [
        GoalCreated(
            GoalPolicy(
                "Offline intervention accounting",
                "a" * 64,
                1000,
                1000,
                clarifications=(ClarificationPolicy("design", "Which layout?"),),
            )
        ),
        QuestionOpened("design", 100),
        QuestionAnswered("design", "Use the existing layout", 50),
        ControlRequested("pause"),
        ControlAcknowledged("pause"),
        ControlRequested("resume"),
    ]
    state = journal.load(GOAL)
    for event in events:
        state = journal.append(GOAL, journal.load(GOAL).sequence, event)
    metrics = qualification.trial_metrics(
        tmp_path, replace(state, action_rejections=2), plan.cases[1]
    )
    assert metrics["interventions"] == 3
    assert metrics["protocol_rejections"] == 2
    assert not metrics["scenario_evidence"]


def test_paused_qualification_reaches_frozen_timeout_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan: qualification.QualificationPlan
) -> None:
    cancelled: list[Path] = []

    class Service:
        def __init__(self, root: Path) -> None:
            root.mkdir()
            self.root = self.directory = root
            self.harness = SimpleNamespace()
            self.journal = SQLiteGoalJournal(root / "goals.sqlite")

        def create(self, _config: RepositoryGoalConfig) -> GoalState:
            return GoalState(phase="ready", paused=True)

        def coordinator(self, _supervisor: object) -> SimpleNamespace:
            return SimpleNamespace(step=expired_step)

        def cancel(self) -> GoalState:
            cancelled.append(self.root)
            return GoalState(phase="cancelled")

        def close(self) -> None:
            pass

    ticks = itertools.count(step=31)
    monkeypatch.setattr(
        qualification, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=no_action)
    )
    monkeypatch.setattr(qualification, "RepositoryGoalService", Service)
    monkeypatch.setattr(goal_bootstrap, "GeminiSupervisor", offline_provider)
    monkeypatch.setattr(goal_bootstrap, "load_gemini_key", offline_key)

    def fake_metrics(
        _root: Path, _state: GoalState, _case: qualification.QualificationCase
    ) -> dict[str, object]:
        return {"interventions": 0, "protocol_rejections": 0, "scenario_evidence": False}

    monkeypatch.setattr(qualification, "trial_metrics", fake_metrics)

    def fake_report(_path: Path) -> dict[str, object]:
        return {"paid_spent_microusd": 0, "paid_reserved_microusd": 0}

    monkeypatch.setattr(qualification, "morning_report", fake_report)
    result = qualification.qualify(plan, tmp_path / "trial-artifacts", tmp_path / "unused-key")
    assert len(cancelled) == len(plan.cases) * plan.repetitions
    assert not result["qualified"]
    assert all(
        row["timed_out"] and not row["complete"]
        for row in cast("list[dict[str,object]]", result["results"])
    )


def test_expired_heartbeat_reports_offline_without_disarming_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(goal_runner.time, "time", lambda: 100)
    goal_runner.record_runner(tmp_path, armed=True, status="running")
    assert goal_runner.runner_record(tmp_path)["status"] == "running"
    monkeypatch.setattr(goal_runner.time, "time", lambda: 116)
    record = goal_runner.runner_record(tmp_path)
    assert record["status"] == "offline"
    assert record["armed"]
    goal_runner.record_runner(tmp_path, armed=True, status="running")
    assert goal_runner.runner_record(tmp_path)["status"] == "running"


def test_execution_lease_prevents_duplicate_drives_and_releases_after_close(tmp_path: Path) -> None:
    directory = tmp_path / "goal"
    directory.mkdir()
    goal_runner.record_runner(directory, armed=True, status="queued")
    first_started, second_started = threading.Event(), threading.Event()

    def first_drive(_directory: Path, stop: threading.Event) -> None:
        first_started.set()
        stop.wait(2)

    def second_drive(_directory: Path, stop: threading.Event) -> None:
        second_started.set()
        stop.wait(2)

    first = goal_runner.GoalExecutionService(tmp_path, check=lambda _: None, drive=first_drive)
    second = goal_runner.GoalExecutionService(tmp_path, check=lambda _: None, drive=second_drive)
    try:
        first.start()
        assert first_started.wait(1)
        second.start()
        assert second.thread is not None
        assert second.thread.is_alive()
        assert not second_started.is_set()
        first.close()
        assert second_started.wait(1)
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("corrupt", [False, True], ids=["schema-not-published", "corrupt-file"])
def test_unreadable_runner_record_does_not_kill_other_queued_goals(
    tmp_path: Path, corrupt: bool
) -> None:
    damaged = tmp_path / "a-damaged"
    damaged.mkdir()
    if corrupt:
        (damaged / "runner.sqlite").write_text("This is not a SQLite database")
    else:
        with sqlite3.connect(damaged / "runner.sqlite"):
            pass
    record = goal_runner.runner_record(damaged)
    assert record["status"] == "offline"
    assert not record["armed"]
    ready = tmp_path / "b-ready"
    ready.mkdir()
    goal_runner.record_runner(ready, armed=True, status="queued")
    executed = threading.Event()

    def drive(_directory: Path, _stop: threading.Event) -> None:
        executed.set()

    service = goal_runner.GoalExecutionService(tmp_path, check=lambda _: None, drive=drive)
    try:
        service.start()
        assert executed.wait(1)
        assert service.thread is not None
        assert service.thread.is_alive()
    finally:
        service.close()


def test_inflight_heartbeat_cannot_overwrite_final_waiting_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "goal"
    directory.mkdir()
    goal_runner.record_runner(directory, armed=True, status="queued")
    heartbeat_entered, heartbeat_written, final_written = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    pulse_stops: list[threading.Event] = []
    real_thread = threading.Thread
    real_record = goal_runner.record_runner

    def thread_factory(  # noqa: PLR0913 - mirrors threading.Thread factory
        group: None = None,
        target: Callable[..., object] | None = None,
        name: str | None = None,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
        *,
        daemon: bool | None = None,
    ) -> threading.Thread:
        if isinstance(target, FunctionType) and target.__name__ == "heartbeat":
            defaults = target.__defaults__
            assert defaults is not None
            assert isinstance(defaults[1], threading.Event)
            pulse_stops.append(defaults[1])
        return real_thread(
            group=group, target=target, name=name, args=args, kwargs=kwargs, daemon=daemon
        )

    def record(path: Path, *, armed: bool, status: str, error: str = "") -> None:
        pulse = status == "running" and threading.current_thread().name != "loopforge-goal-service"
        if pulse:
            heartbeat_entered.set()
            assert pulse_stops[0].wait(2), "Runner did not stop its pulse before releasing work"
        real_record(path, armed=armed, status=status, error=error)
        if pulse:
            heartbeat_written.set()
        if status == "waiting":
            final_written.set()

    def drive(_directory: Path, _stop: threading.Event) -> str:
        assert heartbeat_entered.wait(4), "Runner heartbeat was never issued"
        return "waiting"

    monkeypatch.setattr(goal_runner.threading, "Thread", thread_factory)
    monkeypatch.setattr(goal_runner, "record_runner", record)
    service = goal_runner.GoalExecutionService(tmp_path, check=lambda _: None, drive=drive)
    try:
        service.start()
        assert final_written.wait(5)
        assert heartbeat_written.wait(1)
        assert goal_runner.runner_record(directory)["status"] == "waiting"
    finally:
        service.close()


def test_unexpected_drive_defect_is_recorded_and_the_queue_continues(tmp_path: Path) -> None:
    broken, ready = tmp_path / "a-broken", tmp_path / "b-ready"
    for directory in (broken, ready):
        directory.mkdir()
        goal_runner.record_runner(directory, armed=True, status="queued")
    executed = threading.Event()

    def drive(directory: Path, _stop: threading.Event) -> None:
        if directory == broken:
            msg = "defect"
            raise AssertionError(msg)
        executed.set()

    service = goal_runner.GoalExecutionService(tmp_path, check=lambda _: None, drive=drive)
    try:
        service.start()
        assert executed.wait(1)
        assert service.thread is not None
        assert service.thread.is_alive()
    finally:
        service.close()
    record = goal_runner.runner_record(broken)
    assert record["status"] == "offline"
    assert record["error"] == "Runner failed: AssertionError"
    assert goal_runner.runner_record(ready)["status"] == "stopped"


def test_dedicated_runner_drives_only_its_own_goal(tmp_path: Path) -> None:
    other, mine = tmp_path / "a-other", tmp_path / "b-mine"
    for directory in (other, mine):
        directory.mkdir()
        goal_runner.record_runner(directory, armed=True, status="queued")
    calls: list[Path] = []
    done = threading.Event()

    def drive(directory: Path, _stop: threading.Event) -> None:
        calls.append(directory)
        done.set()

    service = goal_runner.GoalExecutionService(
        tmp_path, check=lambda _: None, drive=drive, only=mine
    )
    try:
        service.start()
        assert done.wait(1)
    finally:
        service.close()
    assert calls == [mine.resolve()]
    assert goal_runner.runner_record(other)["status"] == "queued"


def test_dedicated_runner_refuses_other_goals(tmp_path: Path) -> None:
    mine, other = tmp_path / "mine", tmp_path / "other"
    other.mkdir()
    (other / "goals.sqlite").touch()
    service = goal_runner.GoalExecutionService(tmp_path, check=lambda _: None, only=mine)
    with pytest.raises(ValueError, match="only its own goal"):
        service.arm(other)
    assert goal_runner.runner_record(other) == {"status": "offline", "armed": False}
    with pytest.raises(ValueError, match="under its root"):
        goal_runner.GoalExecutionService(tmp_path / "x", check=lambda _: None, only=mine)
