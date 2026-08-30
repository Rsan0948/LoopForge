"""Unit tests for the durable event-sourced orchestrator (PACS-013)."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

import pytest

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import FixedClock
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.orchestrator import Orchestrator, WorkerBinding
from loopforge.domain.artifacts import ArtifactKind
from loopforge.domain.events import (
    RunStopped,
    VerificationFailed,
    VerificationPassed,
    WorkerMerged,
    WorkerSpawned,
    WorkerStopped,
)
from loopforge.domain.orchestration import MergeOutcome, WorkerOutcome
from loopforge.domain.state import RunState, replay
from loopforge.domain.types import BudgetLimit, RunId, RunStatus, StopReason, WorkerId, WorkspaceId
from loopforge.ports.artifacts import RunArtifact
from loopforge.ports.verifier import VerificationResult

NOW = datetime(2026, 8, 28, tzinfo=UTC)


class FakeRuntime:
    """Scripted per-cycle states; no event persistence (orchestrator owns streams)."""

    def __init__(self, statuses: list[RunStatus], *, cost_usd: float = 0.0) -> None:
        self._statuses = deque(statuses)
        self._cost_usd = cost_usd
        self.steps = 0
        self.cancelled_with: str | None = None

    def start(self, objective: str) -> RunId:
        return RunId(f"run-{objective}")

    def step(self, run_id: RunId) -> RunState:
        self.steps += 1
        return RunState(run_id=run_id, status=self._statuses.popleft(), cost_usd=self._cost_usd)

    def cancel(self, run_id: RunId, *, summary: str = "cancelled by operator") -> RunState:
        self.cancelled_with = summary
        return RunState(run_id=run_id, status=RunStatus.CANCELLED, cost_usd=self._cost_usd)


class FakeVerifier:
    def __init__(self, result: VerificationResult) -> None:
        self._result = result
        self.calls = 0

    def verify(self, state: RunState) -> VerificationResult:
        del state  # the fake verdict is fixed at wiring time
        self.calls += 1
        return self._result


class FakeArtifacts:
    def __init__(self, artifacts: tuple[RunArtifact, ...] = ()) -> None:
        self._artifacts = artifacts
        self.calls = 0

    def collect(self, state: RunState) -> tuple[RunArtifact, ...]:
        del state  # the fake evidence is fixed at wiring time
        self.calls += 1
        return self._artifacts


def _binding(
    worker_id: str,
    runtime: FakeRuntime,
    *,
    share: float = 0.5,
    reconcile: object = ...,
) -> WorkerBinding:
    return WorkerBinding(
        worker_id=WorkerId(worker_id),
        workspace_id=WorkspaceId(f"ws-{worker_id}"),
        objective=f"repair {worker_id}",
        budget_share_cost_usd=share,
        runtime=runtime,
        reconcile=(lambda: f"rev-{worker_id}") if reconcile is ... else reconcile,  # type: ignore[arg-type]
    )


def _orchestrator(  # noqa: PLR0913 - test wiring keeps every seam explicit
    workers: tuple[WorkerBinding, ...],
    *,
    store: InMemoryEventStore | None = None,
    verifier: FakeVerifier | None = None,
    artifacts: FakeArtifacts | None = None,
    telemetry: InMemoryTelemetry | None = None,
    budget: BudgetLimit | None = None,
    max_workers: int = 4,
) -> tuple[Orchestrator, InMemoryEventStore]:
    event_store = store or InMemoryEventStore()
    orchestrator = Orchestrator(
        store=event_store,
        clock=FixedClock(NOW),
        verifier=verifier or FakeVerifier(VerificationResult(passed=True, summary="all green")),
        budget=budget or BudgetLimit(max_cost_usd=1.0, max_iterations=8),
        workers=workers,
        telemetry=telemetry,
        artifacts=artifacts,
        max_workers=max_workers,
    )
    return orchestrator, event_store


def _event_names(store: InMemoryEventStore, run_id: RunId) -> list[str]:
    return [type(event).__name__ for event in store.events_for(run_id)]


def test_orchestrator_spawns_interleaves_merges_and_verifies() -> None:
    first = FakeRuntime([RunStatus.READY, RunStatus.SUCCEEDED])
    second = FakeRuntime([RunStatus.SUCCEEDED])
    merges: list[str] = []
    orchestrator, store = _orchestrator(
        (
            _binding("one", first, reconcile=lambda: merges.append("one") or "rev-one"),
            _binding("two", second, reconcile=lambda: merges.append("two") or "rev-two"),
        )
    )

    state = orchestrator.run(
        "repair the calculator", "worker one: adder.py; worker two: greeter.py"
    )

    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED
    assert first.steps == 2
    assert second.steps == 1
    assert merges == ["one", "two"]
    assert _event_names(store, state.run_id) == [
        "RunStarted",
        "PlanCreated",
        "WorkerSpawned",
        "WorkerSpawned",
        "WorkerStopped",
        "WorkerStopped",
        "WorkerMerged",
        "WorkerMerged",
        "VerificationPassed",
        "RunStopped",
    ]
    # The roster projection is replayable worker ownership evidence.
    assert [str(worker.worker_id) for worker in state.workers] == ["one", "two"]
    assert all(worker.outcome is WorkerOutcome.SUCCEEDED for worker in state.workers)
    assert all(worker.merge_outcome is MergeOutcome.MERGED for worker in state.workers)
    assert [worker.workspace_id for worker in state.workers] == [
        WorkspaceId("ws-one"),
        WorkspaceId("ws-two"),
    ]
    # Replay from the durable stream reproduces the exact terminal state.
    assert replay(state.run_id, store.events_for(state.run_id)) == state


def test_orchestrator_records_worker_failure_and_stops_incomplete() -> None:
    runtime = FakeRuntime([RunStatus.BUDGET_EXHAUSTED])
    orchestrator, store = _orchestrator(
        (_binding("one", runtime, reconcile=lambda: "must-not-run"),)
    )

    state = orchestrator.run("objective", "plan")

    assert state.status is RunStatus.FAILED
    stopped = [e for e in store.events_for(state.run_id) if isinstance(e, WorkerStopped)]
    assert [e.outcome for e in stopped] == [WorkerOutcome.BUDGET_EXHAUSTED]
    assert not any(isinstance(e, WorkerMerged) for e in store.events_for(state.run_id))
    run_stopped = [e for e in store.events_for(state.run_id) if isinstance(e, RunStopped)][-1]
    assert run_stopped.summary.startswith("WORKER_INCOMPLETE")


def test_orchestrator_stops_explicitly_on_merge_conflict() -> None:
    runtime = FakeRuntime([RunStatus.SUCCEEDED])
    orchestrator, store = _orchestrator((_binding("one", runtime, reconcile=lambda: None),))

    state = orchestrator.run("objective", "plan")

    assert state.status is RunStatus.FAILED
    merged = [e for e in store.events_for(state.run_id) if isinstance(e, WorkerMerged)]
    assert [(e.outcome, e.revision) for e in merged] == [(MergeOutcome.CONFLICT, None)]
    run_stopped = [e for e in store.events_for(state.run_id) if isinstance(e, RunStopped)][-1]
    assert run_stopped.summary.startswith("WORKER_MERGE_CONFLICT")


def test_orchestrator_stops_fail_closed_on_merge_error() -> None:
    def _explode() -> str | None:
        msg = "repository metadata changed"
        raise RuntimeError(msg)

    runtime = FakeRuntime([RunStatus.SUCCEEDED])
    orchestrator, store = _orchestrator((_binding("one", runtime, reconcile=_explode),))

    state = orchestrator.run("objective", "plan")

    assert state.status is RunStatus.FAILED
    run_stopped = [e for e in store.events_for(state.run_id) if isinstance(e, RunStopped)][-1]
    assert run_stopped.summary.startswith("WORKER_MERGE_ERROR")


def test_orchestrator_fails_when_merged_verification_fails() -> None:
    runtime = FakeRuntime([RunStatus.SUCCEEDED])
    verifier = FakeVerifier(VerificationResult(passed=False, summary="tests still fail", score=0.5))
    orchestrator, store = _orchestrator((_binding("one", runtime),), verifier=verifier)

    state = orchestrator.run("objective", "plan")

    assert state.status is RunStatus.FAILED
    assert any(isinstance(e, VerificationFailed) for e in store.events_for(state.run_id))
    run_stopped = [e for e in store.events_for(state.run_id) if isinstance(e, RunStopped)][-1]
    assert run_stopped.summary.startswith("MERGED_VERIFICATION_FAILED")


def test_orchestrator_budget_defense_cancels_remaining_workers() -> None:
    first = FakeRuntime([RunStatus.READY, RunStatus.READY, RunStatus.SUCCEEDED], cost_usd=0.6)
    second = FakeRuntime([RunStatus.READY, RunStatus.SUCCEEDED], cost_usd=0.6)
    orchestrator, store = _orchestrator(
        (_binding("one", first, share=0.5), _binding("two", second, share=0.5)),
        budget=BudgetLimit(max_cost_usd=1.0, max_iterations=8),
    )

    state = orchestrator.run("objective", "plan")

    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert second.cancelled_with is not None or first.cancelled_with is not None
    outcomes = {str(w.worker_id): w.outcome for w in state.workers}
    assert WorkerOutcome.CANCELLED in outcomes.values() or any(
        outcome is WorkerOutcome.BUDGET_EXHAUSTED for outcome in outcomes.values()
    )
    run_stopped = [e for e in store.events_for(state.run_id) if isinstance(e, RunStopped)][-1]
    assert run_stopped.summary.startswith("ORCHESTRATOR_BUDGET_EXHAUSTED")


def test_orchestrator_records_merged_evidence_artifacts() -> None:
    runtime = FakeRuntime([RunStatus.SUCCEEDED])
    artifacts = FakeArtifacts(
        (
            RunArtifact(
                kind=ArtifactKind.WORKSPACE_SNAPSHOT,
                label="workspace:calculator",
                content="merged diff evidence",
            ),
        )
    )
    orchestrator, store = _orchestrator((_binding("one", runtime),), artifacts=artifacts)

    state = orchestrator.run("objective", "plan")

    assert state.status is RunStatus.SUCCEEDED
    assert artifacts.calls == 1
    assert any(type(e).__name__ == "ArtifactRecorded" for e in store.events_for(state.run_id))
    assert [f.label for f in state.recorded_artifacts] == ["workspace:calculator"]


def test_orchestrator_artifact_collection_failure_stops_fail_closed() -> None:
    class ExplodingCollector:
        def collect(self, state: RunState) -> tuple[RunArtifact, ...]:
            del state  # collection fails before any state is consulted
            msg = "collector broke"
            raise RuntimeError(msg)

    runtime = FakeRuntime([RunStatus.SUCCEEDED])
    orchestrator, store = _orchestrator((_binding("one", runtime),))
    orchestrator.artifacts = ExplodingCollector()  # type: ignore[assignment]

    state = orchestrator.run("objective", "plan")

    assert state.status is RunStatus.FAILED
    run_stopped = [e for e in store.events_for(state.run_id) if isinstance(e, RunStopped)][-1]
    assert run_stopped.summary.startswith("artifact collection failed")


def test_orchestrator_telemetry_projects_worker_lifecycle_with_worker_ids() -> None:
    telemetry = InMemoryTelemetry()
    runtime = FakeRuntime([RunStatus.SUCCEEDED])
    orchestrator, _store = _orchestrator((_binding("one", runtime),), telemetry=telemetry)

    state = orchestrator.run("objective", "plan")

    assert state.status is RunStatus.SUCCEEDED
    worker_logs = [
        record
        for record in telemetry.logs
        if record.message in {"worker spawned", "worker stopped", "worker merged"}
    ]
    assert len(worker_logs) == 3
    assert all(record.correlation.worker_id == WorkerId("one") for record in worker_logs)
    worker_metrics = [
        sample for sample in telemetry.metrics if sample.name.startswith("loopforge.workers.")
    ]
    assert {sample.name for sample in worker_metrics} == {
        "loopforge.workers.spawned",
        "loopforge.workers.stopped",
        "loopforge.workers.merged",
    }
    run_spans = [span for span in telemetry.spans if span.name == "loopforge.run"]
    assert run_spans
    assert run_spans[0].attributes["loopforge.run.workers"] == 1


def test_orchestrator_requires_at_least_one_worker() -> None:
    with pytest.raises(ValueError, match="at least one worker"):
        _orchestrator(())


def test_orchestrator_bounds_worker_count() -> None:
    workers = tuple(_binding(f"w{index}", FakeRuntime([RunStatus.SUCCEEDED])) for index in range(3))
    with pytest.raises(ValueError, match="code-owned maximum"):
        _orchestrator(workers, max_workers=2)


def test_orchestrator_rejects_duplicate_worker_ids() -> None:
    workers = (
        _binding("one", FakeRuntime([RunStatus.SUCCEEDED])),
        _binding("one", FakeRuntime([RunStatus.SUCCEEDED])),
    )
    with pytest.raises(ValueError, match="worker ids must be unique"):
        _orchestrator(workers)


def test_orchestrator_rejects_shares_above_global_limit() -> None:
    workers = (
        _binding("one", FakeRuntime([RunStatus.SUCCEEDED]), share=0.7),
        _binding("two", FakeRuntime([RunStatus.SUCCEEDED]), share=0.7),
    )
    with pytest.raises(ValueError, match="never sum above the global limit"):
        _orchestrator(workers)


def test_worker_binding_validates_assignment_fields() -> None:
    runtime = FakeRuntime([RunStatus.SUCCEEDED])
    with pytest.raises(ValueError, match="plain name without path separators"):
        _binding("bad/id", runtime)
    with pytest.raises(ValueError, match="worker objective cannot be empty"):
        WorkerBinding(
            worker_id=WorkerId("one"),
            workspace_id=WorkspaceId("ws-one"),
            objective="   ",
            budget_share_cost_usd=0.5,
            runtime=runtime,
            reconcile=lambda: "rev",
        )
    with pytest.raises(ValueError, match="budget share must be positive"):
        _binding("one", runtime, share=0.0)
    with pytest.raises(TypeError, match="reconcile must be callable"):
        _binding("one", runtime, reconcile="not-callable")


def test_spawned_events_carry_durable_worker_ownership() -> None:
    runtime = FakeRuntime([RunStatus.SUCCEEDED])
    orchestrator, store = _orchestrator((_binding("one", runtime, share=0.25),))

    state = orchestrator.run("objective", "plan")

    spawned = [e for e in store.events_for(state.run_id) if isinstance(e, WorkerSpawned)]
    assert len(spawned) == 1
    assert spawned[0].worker_run_id == RunId("run-repair one")
    assert spawned[0].workspace_id == WorkspaceId("ws-one")
    assert spawned[0].budget_share_cost_usd == 0.25
    passed = [e for e in store.events_for(state.run_id) if isinstance(e, VerificationPassed)]
    assert passed
    assert passed[0].summary == "all green"
