"""One-command unattended goal: create, arm a dedicated runner, drive to a terminal state.

A runner dedicated to this goal never drives another armed goal under the same root.
The supervisor, recording and evidence wiring come from goal_bootstrap, shared with the
console's execution service. The paid ceiling is the intake's own frozen
`max_cost_microusd`; this command cannot raise it.

Usage: loopforge-repository-supervisor launch --directory DIR [--config INTAKE.json]
"""

import json
import signal
import threading
from pathlib import Path
from types import FrameType

from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.entrypoints.goal_review import morning_report
from loopforge.entrypoints.goal_runner import GoalExecutionService, runner_record
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL


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
    runner = GoalExecutionService(directory.parent, only=directory)

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
