"""Minimal deterministic worker orchestration seam (PACS-013)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.orchestration import MergeOutcome, WorkerOutcome, validate_worker_id
from loopforge.domain.state import RunState
from loopforge.domain.types import RunId, RunStatus, WorkerId


class SteppableRuntime(Protocol):
    def start(self, objective: str) -> RunId: ...

    def step(self, run_id: RunId) -> RunState: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerBinding:
    """Code-owned worker assignment and reconciliation callback."""

    worker_id: WorkerId
    objective: str
    runtime: SteppableRuntime
    reconcile: Callable[[], str | None]

    def __post_init__(self) -> None:
        validate_worker_id(str(self.worker_id))
        if not self.objective.strip():
            msg = "worker objective cannot be empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerResult:
    worker_id: WorkerId
    run_id: RunId
    outcome: WorkerOutcome
    merge_outcome: MergeOutcome | None = None
    revision: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OrchestrationResult:
    succeeded: bool
    workers: tuple[WorkerResult, ...]


class RoundRobinOrchestrator:
    """Drive isolated runtimes one cycle at a time, then reconcile in order."""

    def __init__(self, workers: tuple[WorkerBinding, ...], *, max_workers: int = 4) -> None:
        if not workers:
            msg = "orchestrator requires at least one worker"
            raise ValueError(msg)
        if len(workers) > max_workers:
            msg_2 = "worker count exceeds the code-owned maximum"
            raise ValueError(msg_2)
        ids = [worker.worker_id for worker in workers]
        if len(set(ids)) != len(ids):
            msg_3 = "worker ids must be unique"
            raise ValueError(msg_3)
        self._workers = workers

    def run(self) -> OrchestrationResult:
        run_ids = {
            worker.worker_id: worker.runtime.start(worker.objective) for worker in self._workers
        }
        terminal: dict[WorkerId, RunState] = {}
        while len(terminal) < len(self._workers):
            for worker in self._workers:
                if worker.worker_id in terminal:
                    continue
                state = worker.runtime.step(run_ids[worker.worker_id])
                if state.status.is_terminal or state.status is RunStatus.WAITING_FOR_APPROVAL:
                    terminal[worker.worker_id] = state

        results: list[WorkerResult] = []
        for worker in self._workers:
            state = terminal[worker.worker_id]
            outcome = _worker_outcome(state.status)
            if outcome is not WorkerOutcome.SUCCEEDED:
                results.append(
                    WorkerResult(
                        worker_id=worker.worker_id,
                        run_id=run_ids[worker.worker_id],
                        outcome=outcome,
                    )
                )
                continue
            revision = worker.reconcile()
            merge_outcome = MergeOutcome.MERGED if revision is not None else MergeOutcome.CONFLICT
            results.append(
                WorkerResult(
                    worker_id=worker.worker_id,
                    run_id=run_ids[worker.worker_id],
                    outcome=outcome,
                    merge_outcome=merge_outcome,
                    revision=revision,
                )
            )
            if merge_outcome is MergeOutcome.CONFLICT:
                return OrchestrationResult(succeeded=False, workers=tuple(results))
        return OrchestrationResult(
            succeeded=all(
                result.outcome is WorkerOutcome.SUCCEEDED
                and result.merge_outcome is MergeOutcome.MERGED
                for result in results
            ),
            workers=tuple(results),
        )


def _worker_outcome(status: RunStatus) -> WorkerOutcome:
    if status is RunStatus.SUCCEEDED:
        return WorkerOutcome.SUCCEEDED
    if status is RunStatus.CANCELLED:
        return WorkerOutcome.CANCELLED
    if status is RunStatus.BUDGET_EXHAUSTED:
        return WorkerOutcome.BUDGET_EXHAUSTED
    return WorkerOutcome.FAILED
