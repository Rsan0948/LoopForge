"""One-command goal launch: frozen intake, dedicated runner, terminal report; no network."""

import inspect
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from loopforge.entrypoints import goal_launch
from loopforge.entrypoints.goal_runner import GoalExecutionService, record_runner, runner_record
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from tests.unit.test_audit_core_regressions import repository_config as config_fixture


def _config(tmp_path: Path) -> Path:
    build = cast("Callable[[Path], RepositoryGoalConfig]", inspect.unwrap(config_fixture))
    path = tmp_path / "intake.json"
    path.write_text(build(tmp_path).model_dump_json())
    return path


def _runner_without_dependencies(
    monkeypatch: pytest.MonkeyPatch, drive: Callable[[Path, threading.Event], str | None]
) -> None:
    def build(root: Path, **kwargs: object) -> GoalExecutionService:
        only = cast("Path", kwargs["only"])
        return GoalExecutionService(root, check=lambda _: None, drive=drive, only=only)

    monkeypatch.setattr(goal_launch, "GoalExecutionService", build)


def test_a_new_goal_requires_an_intake_and_an_existing_goal_keeps_its_own(
    tmp_path: Path,
) -> None:
    goals = tmp_path / "goals"
    with pytest.raises(ValueError, match="needs --config"):
        goal_launch.launch(goals / "new", None)
    config = _config(tmp_path)
    existing = goals / "existing"
    service = RepositoryGoalService(existing)
    service.create(RepositoryGoalConfig.model_validate_json(config.read_text()))
    service.close()
    with pytest.raises(ValueError, match="cannot be replaced"):
        goal_launch.launch(existing, config)


def test_launch_drives_only_its_goal_to_a_terminal_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goals = tmp_path / "goals"
    sibling = goals / "a-sibling"
    sibling.mkdir(parents=True)
    record_runner(sibling, armed=True, status="queued")
    driven: list[Path] = []

    def drive(directory: Path, _stop: threading.Event) -> None:
        driven.append(directory)
        service = RepositoryGoalService(directory)
        try:
            service.cancel()  # A terminal outcome, as a real drive reaches one.
        finally:
            service.close()

    _runner_without_dependencies(monkeypatch, drive)
    result = goal_launch.launch(goals / "b-mine", _config(tmp_path), poll_seconds=0.05)
    assert result["terminal"] == "cancelled"
    assert driven == [(goals / "b-mine").resolve()]
    assert (goals / "b-mine/final-report.json").is_file()
    assert runner_record(sibling)["status"] == "queued"


def test_launch_reports_a_runner_failure_instead_of_waiting_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drive(_directory: Path, _stop: threading.Event) -> None:
        msg = "defect"
        raise AssertionError(msg)

    _runner_without_dependencies(monkeypatch, drive)
    result = goal_launch.launch(tmp_path / "goals/mine", _config(tmp_path), poll_seconds=0.05)
    assert result == {"stopped": "offline", "error": "Runner failed: AssertionError"}


def test_the_recorded_drive_gives_gemini_the_evidence_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loopforge.adapters.research_store import ResearchStore  # noqa: PLC0415

    directory = tmp_path / "goals/mine"
    service = RepositoryGoalService(directory)
    service.create(RepositoryGoalConfig.model_validate_json(_config(tmp_path).read_text()))
    service.close()
    providers: list[object] = []

    class Provider:
        evidence_store: object = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            providers.append(self)

        def close(self) -> None:
            return None

    def stop_before_any_decision(self: RepositoryGoalService, supervisor: object) -> object:
        del self, supervisor
        msg = "stop"
        raise RuntimeError(msg)

    monkeypatch.setattr(goal_launch, "GeminiSupervisor", Provider)
    def key(_path: Path) -> str:
        return "k"

    monkeypatch.setattr(goal_launch, "load_gemini_key", key)
    monkeypatch.setattr(RepositoryGoalService, "coordinator", stop_before_any_decision)
    with pytest.raises(RuntimeError, match="stop"):
        goal_launch.recorded_drive(directory.resolve(), threading.Event())
    assert isinstance(cast("Provider", providers[0]).evidence_store, ResearchStore)
