from __future__ import annotations

from collections import deque

from loopforge.application.orchestrator import RoundRobinOrchestrator, WorkerBinding
from loopforge.domain.orchestration import MergeOutcome, WorkerOutcome
from loopforge.domain.state import RunState
from loopforge.domain.types import RunId, RunStatus, WorkerId


class FakeRuntime:
    def __init__(self, statuses: list[RunStatus]) -> None:
        self._statuses = deque(statuses)
        self.steps = 0

    def start(self, objective: str) -> RunId:
        return RunId(f"run-{objective}")

    def step(self, run_id: RunId) -> RunState:
        self.steps += 1
        return RunState(run_id=run_id, status=self._statuses.popleft())


def test_orchestrator_steps_round_robin_and_merges_in_spawn_order() -> None:
    first = FakeRuntime([RunStatus.READY, RunStatus.SUCCEEDED])
    second = FakeRuntime([RunStatus.SUCCEEDED])
    merged: list[str] = []
    orchestrator = RoundRobinOrchestrator(
        (
            WorkerBinding(
                worker_id=WorkerId("one"),
                objective="one",
                runtime=first,
                reconcile=lambda: merged.append("one") or "rev-one",
            ),
            WorkerBinding(
                worker_id=WorkerId("two"),
                objective="two",
                runtime=second,
                reconcile=lambda: merged.append("two") or "rev-two",
            ),
        )
    )

    result = orchestrator.run()

    assert result.succeeded
    assert first.steps == 2
    assert second.steps == 1
    assert merged == ["one", "two"]
    assert all(item.merge_outcome is MergeOutcome.MERGED for item in result.workers)


def test_orchestrator_reports_failure_without_merging_failed_worker() -> None:
    runtime = FakeRuntime([RunStatus.BUDGET_EXHAUSTED])
    orchestrator = RoundRobinOrchestrator(
        (
            WorkerBinding(
                worker_id=WorkerId("one"),
                objective="one",
                runtime=runtime,
                reconcile=lambda: "must-not-run",
            ),
        )
    )

    result = orchestrator.run()

    assert not result.succeeded
    assert result.workers[0].outcome is WorkerOutcome.BUDGET_EXHAUSTED
    assert result.workers[0].merge_outcome is None


def test_orchestrator_stops_reconciliation_on_conflict() -> None:
    runtime = FakeRuntime([RunStatus.SUCCEEDED])
    result = RoundRobinOrchestrator(
        (
            WorkerBinding(
                worker_id=WorkerId("one"),
                objective="one",
                runtime=runtime,
                reconcile=lambda: None,
            ),
        )
    ).run()

    assert not result.succeeded
    assert result.workers[0].merge_outcome is MergeOutcome.CONFLICT
