"""An operator stop reaches an in-flight coding run at its next step boundary."""

import threading
from dataclasses import replace
from pathlib import Path

from loopforge.domain.state import RunState
from loopforge.domain.types import RunId, RunStatus
from loopforge.entrypoints.campaign import CampaignHarness


class StubRuntime:
    def __init__(self, *, stop_after: int | None, event: threading.Event) -> None:
        self.steps = 0
        self.cancelled: str | None = None
        self.state = RunState(RunId("run_stop"), objective="x", status=RunStatus.READY)
        self.stop_after, self.event = stop_after, event

    def state_for(self, run_id: RunId) -> RunState:
        del run_id
        return self.state

    def step(self, run_id: RunId) -> RunState:
        del run_id
        self.steps += 1
        if self.stop_after is not None and self.steps == self.stop_after:
            self.event.set()  # operator presses Ctrl+C mid-run
        if self.steps >= 5:
            self.state = replace(self.state, status=RunStatus.SUCCEEDED)
        return self.state

    def cancel(self, run_id: RunId, *, summary: str) -> RunState:
        del run_id
        self.cancelled = summary
        self.state = replace(self.state, status=RunStatus.CANCELLED)
        return self.state


def test_operator_stop_cancels_at_the_next_step_boundary(tmp_path: Path) -> None:
    harness = CampaignHarness(tmp_path)
    stop = threading.Event()
    harness.operator_stop = stop
    runtime = StubRuntime(stop_after=2, event=stop)
    result = harness.drive_runtime(runtime, RunId("run_stop"))  # type: ignore[arg-type]
    assert result.status is RunStatus.CANCELLED
    assert runtime.cancelled == "operator stop requested"
    assert runtime.steps == 2


def test_without_a_stop_the_run_proceeds_to_its_own_outcome(tmp_path: Path) -> None:
    harness = CampaignHarness(tmp_path)
    harness.operator_stop = threading.Event()
    runtime = StubRuntime(stop_after=None, event=threading.Event())
    result = harness.drive_runtime(runtime, RunId("run_stop"))  # type: ignore[arg-type]
    assert result.status is RunStatus.SUCCEEDED
    assert runtime.cancelled is None
    assert runtime.steps == 5
