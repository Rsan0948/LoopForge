"""One-command unattended goal: create, arm a dedicated runner, drive to a terminal state.

This is the trial runners' validated composition (`.loopforge/tasks/*/run.py`) as a
product command. A runner dedicated to this goal never drives another armed goal under
the same root. Supervisor prompts and worker requests are recorded beside the goal for
review. The paid ceiling is the intake's own frozen `max_cost_microusd`; this command
cannot raise it.

Usage: loopforge-repository-supervisor launch --directory DIR [--config INTAKE.json]
"""

import json
import signal
import threading
from pathlib import Path
from types import FrameType

from loopforge.adapters.gemini_supervisor import GeminiSupervisor, load_gemini_key
from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.domain.goals import GoalState
from loopforge.entrypoints.autonomy_qualification import RecordedSupervisor
from loopforge.entrypoints.goal_review import morning_report
from loopforge.entrypoints.goal_runner import GoalExecutionService, runner_record
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL

KEY_PATH = Path(".loopforge/secrets.env")


def recorded_drive(directory: Path, stop: threading.Event) -> str | None:
    """Drive one goal with request capture, as the delivered trials did."""
    service = RepositoryGoalService(directory)
    provider = None
    try:
        service.harness.nonblocking_goals = True
        service.harness.operator_stop = stop
        service.harness.provider_request_directory = directory / "worker-prompts"
        service.worker().recover_interrupted()
        state = service.journal.load(GOAL)
        if state.terminal:
            return None
        if state.paused or state.late_answer_conflict:
            return "waiting"
        provider = GeminiSupervisor(
            load_gemini_key(KEY_PATH),
            count_tokens=True,
            diagnostics=directory / "supervisor-diagnostics",
        )
        coordinator = service.coordinator(
            RecordedSupervisor(provider, directory / "supervisor-prompts")
        )
        while not stop.is_set():
            before = service.journal.load(GOAL)
            state = coordinator.step(GOAL)
            if state.sequence != before.sequence:
                print(json.dumps(progress(state)), flush=True)
            if state.terminal:
                return None
            if state.paused or state.late_answer_conflict:
                return "waiting"
            stop.wait(1)
    finally:
        if provider:
            provider.close()
        service.close()
    return None


def progress(state: GoalState) -> dict[str, object]:
    return {
        "sequence": state.sequence,
        "phase": state.phase,
        "decisions": state.decisions,
        "action": state.decision.action if state.decision else None,
        "worker": state.observation.status,
        "spent_microusd": state.spent_microusd,
        "summary": state.summary,
    }


def launch(directory: Path, config: Path | None, *, poll_seconds: float = 2) -> dict[str, object]:
    directory = directory.resolve()
    if not (directory / "goals.sqlite").is_file():
        if config is None:
            msg = "A new goal needs --config with a trusted repository intake"
            raise ValueError(msg)
        service = RepositoryGoalService(directory)
        try:
            service.create(RepositoryGoalConfig.model_validate_json(config.read_text()))
        finally:
            service.close()
    elif config is not None:
        msg = "The goal already exists; its frozen intake cannot be replaced"
        raise ValueError(msg)
    stop = threading.Event()
    runner = GoalExecutionService(directory.parent, drive=recorded_drive, only=directory)

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    previous = [signal.signal(s, interrupt) for s in (signal.SIGINT, signal.SIGTERM)]
    try:
        runner.arm(directory)
        while not stop.wait(poll_seconds):
            state = SQLiteGoalJournal(directory / "goals.sqlite").load(GOAL)
            record = runner_record(directory)
            if state.terminal and record["status"] == "stopped":
                report = morning_report(directory)
                (directory / "final-report.json").write_text(
                    json.dumps(report, indent=2, default=str)
                )
                return {"terminal": state.phase, "summary": state.summary}
            if record["status"] in {"offline", "waiting"} and record.get("error"):
                return {"stopped": record["status"], "error": record["error"]}
            if record["status"] == "waiting":
                return {"waiting": True, "summary": state.summary}
        return {"interrupted": True}
    finally:
        runner.close()
        for s, handler in zip((signal.SIGINT, signal.SIGTERM), previous, strict=True):
            signal.signal(s, handler)
