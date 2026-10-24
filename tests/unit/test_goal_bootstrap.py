"""One goal composition for every entry path; no network (the provider never sends)."""

import inspect
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from loopforge.adapters.research_store import ResearchStore
from loopforge.domain.goals import GoalState
from loopforge.entrypoints import goal_bootstrap, goal_runner
from loopforge.entrypoints.goal_bootstrap import GoalWiring, drive_goal, wire
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from tests.unit.test_audit_core_regressions import repository_config as config_fixture


def _never_called(request: httpx.Request) -> httpx.Response:
    del request
    msg = "composition must not call the provider"
    raise AssertionError(msg)


@pytest.fixture
def goal(tmp_path: Path) -> RepositoryGoalService:
    build = cast("Callable[[Path], RepositoryGoalConfig]", inspect.unwrap(config_fixture))
    service = RepositoryGoalService(tmp_path / "goals" / "one")
    service.create(build(tmp_path))
    return service


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "secrets.env"
    path.write_text("GEMINI_API_KEY=offline-fixture-key\n")
    return path


@pytest.mark.parametrize("record", [True, False])
def test_every_wiring_gives_the_real_provider_the_evidence_store(
    goal: RepositoryGoalService, key_file: Path, record: bool
) -> None:
    with httpx.Client(transport=httpx.MockTransport(_never_called)) as client:
        stack = wire(
            goal, GoalWiring(key_file=key_file, record=record, count_tokens=False), client=client
        )
        try:
            assert isinstance(stack.provider.evidence_store, ResearchStore)
            assert goal.harness.nonblocking_goals
            recorded = goal.harness.provider_request_directory
            assert (recorded == goal.directory / "worker-prompts") is record
            assert (goal.directory / "supervisor-prompts").is_dir() is record
        finally:
            stack.close()
            goal.close()


def test_a_wrapper_that_drops_evidence_attachment_is_refused(
    goal: RepositoryGoalService, key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Forgetful:
        """A decorator without attach_evidence: the old silent fallback to excerpts."""

        def __init__(self, delegate: object, directory: Path) -> None:
            del directory
            self.delegate = delegate

        def prepare(self, state: GoalState) -> object:
            return state

        def decide(self, request: object) -> object:
            return request

    monkeypatch.setattr(goal_bootstrap, "RecordedSupervisor", Forgetful)
    with httpx.Client(transport=httpx.MockTransport(_never_called)) as client:
        try:
            with pytest.raises(ValueError, match="lost the evidence store"):
                wire(goal, GoalWiring(key_file=key_file, count_tokens=False), client=client)
        finally:
            goal.close()


@pytest.mark.parametrize(("dedicated", "yields"), [(True, False), (False, True)])
def test_launch_and_the_console_queue_share_one_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dedicated: bool, yields: bool
) -> None:
    calls: list[tuple[Path, bool]] = []

    def drive(directory: Path, stop: threading.Event, *, yield_when_idle: bool) -> None:
        del stop
        calls.append((directory, yield_when_idle))

    monkeypatch.setattr(goal_runner, "drive_goal", drive)
    goal_dir = tmp_path / "goal"
    service = goal_runner.GoalExecutionService(
        tmp_path, check=lambda _: None, only=goal_dir if dedicated else None
    )
    service._drive(goal_dir, threading.Event())  # pyright: ignore[reportPrivateUsage]
    assert calls == [(goal_dir, yields)]


class _Ticks(threading.Event):
    """Stops the drive after a fixed number of idle waits, without sleeping."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit, self.waits = limit, 0

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.waits += 1
        if self.waits >= self.limit:
            self.set()
        return self.is_set()


@pytest.mark.parametrize(
    ("yield_when_idle", "expected", "steps"), [(True, "waiting", 1), (False, None, 3)]
)
def test_only_a_shared_queue_yields_an_idle_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    yield_when_idle: bool,
    expected: str | None,
    steps: int,
) -> None:
    idle = GoalState(phase="ready")
    stepped: list[GoalState] = []

    def load(goal: str) -> GoalState:
        del goal
        return idle

    journal = SimpleNamespace(load=load)
    service = SimpleNamespace(
        directory=tmp_path,
        harness=SimpleNamespace(),
        journal=journal,
        worker=lambda: SimpleNamespace(recover_interrupted=lambda: None),
        close=lambda: None,
    )

    def step(_goal: str) -> GoalState:
        stepped.append(idle)
        return idle

    stack = SimpleNamespace(coordinator=SimpleNamespace(step=step), close=lambda: None)

    def open_service(directory: Path) -> SimpleNamespace:
        del directory
        return service

    def wired(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return stack

    monkeypatch.setattr(goal_bootstrap, "goal_service", open_service)
    monkeypatch.setattr(goal_bootstrap, "wire", wired)
    outcome = drive_goal(tmp_path, _Ticks(3), yield_when_idle=yield_when_idle)
    assert outcome == expected
    assert len(stepped) == steps
