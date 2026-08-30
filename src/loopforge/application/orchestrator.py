"""Deterministic event-sourced worker orchestration (PACS-013).

The orchestrator owns the global plan and its own authoritative run stream;
workers own assigned workspaces and per-worker run streams on the shared
event store. Execution is deterministic interleaving: worker runtimes are
driven one cycle at a time in a fixed code-owned order (logical concurrency,
zero wall-clock races, trivial replay).

Worker ownership, terminal outcomes, and merge results are durable domain
events on the orchestrator stream (``WorkerSpawned``/``WorkerStopped``/
``WorkerMerged`` — the operator-signed-off schema-v1 catalog extension), so
an orchestrated run replays exactly like a single-runtime run. Workers get
static budget *shares* of the global limit enforced by their own
``ControlPolicy`` (never new authority — AGENTS.md rule 12); the orchestrator
adds only a defense-in-depth aggregate check. Reconciliation merges succeeded
workers in spawn order; a conflict aborts that worker's branch and stops the
run ``FAILURE`` explicitly — never a silent overwrite.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from loopforge.application.telemetry import RuntimeTelemetry
from loopforge.domain.events import (
    ArtifactRecorded,
    Event,
    PlanCreated,
    RunStarted,
    RunStopped,
    VerificationFailed,
    VerificationPassed,
    WorkerMerged,
    WorkerSpawned,
    WorkerStopped,
)
from loopforge.domain.orchestration import (
    MergeOutcome,
    WorkerOutcome,
    validate_budget_share,
    validate_worker_id,
    validate_worker_text,
)
from loopforge.domain.policy import ControlPolicy
from loopforge.domain.state import RunState, replay
from loopforge.domain.types import (
    BudgetLimit,
    EventId,
    RunId,
    RunStatus,
    StopReason,
    WorkerId,
    WorkspaceId,
)
from loopforge.ports.artifacts import ArtifactCollectorPort, ArtifactContractError, RunArtifact
from loopforge.ports.clock import ClockPort
from loopforge.ports.state_store import StateStorePort
from loopforge.ports.telemetry import TelemetryPort
from loopforge.ports.verifier import VerificationResult, VerifierContractError, VerifierPort

EventFactory = Callable[[EventId, RunId, datetime, int], Event]


class SteppableRuntime(Protocol):
    """The per-cycle drive seam an orchestrated runtime exposes (Runtime)."""

    def start(self, objective: str) -> RunId: ...

    def step(self, run_id: RunId) -> RunState: ...

    def cancel(self, run_id: RunId, *, summary: str = "cancelled by operator") -> RunState: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerBinding:
    """Code-owned worker assignment and reconciliation callback.

    ``budget_share_cost_usd`` is this worker's static share of the global
    cost limit, recorded durably at spawn (the worker's own ``ControlPolicy``
    enforces the full share; enforcement never moves here). ``reconcile``
    merges the worker's verified patch into the integration workspace and
    returns the merge revision, or ``None`` when the merge conflicts and was
    aborted.
    """

    worker_id: WorkerId
    workspace_id: WorkspaceId
    objective: str
    budget_share_cost_usd: float
    runtime: SteppableRuntime
    reconcile: Callable[[], str | None]

    def __post_init__(self) -> None:
        validate_worker_id(str(self.worker_id))
        validate_worker_id(str(self.workspace_id))
        validate_worker_text(self.objective, "worker objective")
        validate_budget_share(self.budget_share_cost_usd)
        if not callable(self.reconcile):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "worker reconcile must be callable"
            raise TypeError(msg)


@dataclass(slots=True)
class Orchestrator:
    """Drive isolated worker runtimes and reconcile their verified patches.

    The orchestrator holds no model, no tools, and no budget authority: every
    model turn happens inside a worker's own runtime under that worker's
    ``ControlPolicy``. The global ``budget`` is used only for a
    defense-in-depth aggregate cost check (static shares already sum to no
    more than the global limit) and for the code-owned success/budget stop
    decision after integration verification.
    """

    store: StateStorePort
    clock: ClockPort
    verifier: VerifierPort
    budget: BudgetLimit
    workers: tuple[WorkerBinding, ...]
    telemetry: TelemetryPort | None = None
    artifacts: ArtifactCollectorPort | None = None
    max_workers: int = 4
    _telemetry: RuntimeTelemetry = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.workers:
            msg = "orchestrator requires at least one worker"
            raise ValueError(msg)
        if len(self.workers) > self.max_workers:
            msg_2 = "worker count exceeds the code-owned maximum"
            raise ValueError(msg_2)
        ids = [str(worker.worker_id) for worker in self.workers]
        if len(set(ids)) != len(ids):
            msg_3 = "worker ids must be unique"
            raise ValueError(msg_3)
        shares = sum(worker.budget_share_cost_usd for worker in self.workers)
        if shares > self.budget.max_cost_usd:
            msg_4 = "worker budget shares must never sum above the global limit"
            raise ValueError(msg_4)
        # Telemetry is optional and always fail-safe, exactly like Runtime.
        self._telemetry = RuntimeTelemetry(self.telemetry, self.clock)

    def run(self, objective: str, plan: str) -> RunState:  # noqa: PLR0911 - flat auditable stops
        """Execute the global plan: spawn, interleave, reconcile, verify."""
        run_id = RunId(f"run_{uuid4().hex[:12]}")
        self._persist(
            run_id,
            lambda event_id, rid, occurred_at, sequence: RunStarted(
                event_id=event_id,
                run_id=rid,
                occurred_at=occurred_at,
                sequence=sequence,
                objective=objective,
            ),
        )
        self._persist_plan(run_id, plan)

        run_ids = {
            worker.worker_id: worker.runtime.start(worker.objective) for worker in self.workers
        }
        for worker in self.workers:
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence, worker=worker: WorkerSpawned(
                    event_id=event_id,
                    run_id=rid,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    worker_id=worker.worker_id,
                    worker_run_id=run_ids[worker.worker_id],
                    workspace_id=worker.workspace_id,
                    objective=worker.objective,
                    budget_share_cost_usd=worker.budget_share_cost_usd,
                ),
            )

        budget_tripped, latest = self._interleave(run_ids)
        outcomes = self._record_outcomes(run_id, latest)

        if budget_tripped:
            self._stop(
                run_id,
                StopReason.BUDGET_EXHAUSTED,
                "ORCHESTRATOR_BUDGET_EXHAUSTED",
                summary=(
                    "ORCHESTRATOR_BUDGET_EXHAUSTED: aggregate worker cost reached the global limit"
                ),
            )
            return self.state_for(run_id)
        incomplete = [
            f"{worker_id} ({outcome.value})"
            for worker_id, outcome in outcomes.items()
            if outcome is not WorkerOutcome.SUCCEEDED
        ]
        if incomplete:
            self._stop(
                run_id,
                StopReason.FAILURE,
                "WORKER_INCOMPLETE",
                summary=f"WORKER_INCOMPLETE: {', '.join(incomplete)}",
            )
            return self.state_for(run_id)

        for worker in self.workers:
            try:
                revision = worker.reconcile()
            except Exception as exc:
                self._stop(
                    run_id,
                    StopReason.FAILURE,
                    "WORKER_MERGE_ERROR",
                    summary=_bounded(f"WORKER_MERGE_ERROR: {type(exc).__name__}: {exc}"),
                )
                return self.state_for(run_id)
            if revision is None:
                self._persist(
                    run_id,
                    lambda event_id, rid, occurred_at, sequence, worker=worker: WorkerMerged(
                        event_id=event_id,
                        run_id=rid,
                        occurred_at=occurred_at,
                        sequence=sequence,
                        worker_id=worker.worker_id,
                        outcome=MergeOutcome.CONFLICT,
                        revision=None,
                        detail=f"merge of worker/{worker.worker_id} conflicted and was aborted",
                    ),
                )
                self._stop(
                    run_id,
                    StopReason.FAILURE,
                    "WORKER_MERGE_CONFLICT",
                    summary=f"WORKER_MERGE_CONFLICT: worker {worker.worker_id} branch conflicts",
                )
                return self.state_for(run_id)
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence, worker=worker, revision=revision: (
                    WorkerMerged(
                        event_id=event_id,
                        run_id=rid,
                        occurred_at=occurred_at,
                        sequence=sequence,
                        worker_id=worker.worker_id,
                        outcome=MergeOutcome.MERGED,
                        revision=revision,
                        detail=f"merged worker/{worker.worker_id} at {revision[:12]}",
                    )
                ),
            )

        # Every worker stopped and every succeeded worker merged: the reducer
        # has moved the orchestrated run to VERIFYING. Integration
        # verification runs against the merged workspace — verifier truth is
        # code-owned, exactly like a single-runtime run.
        verification = self._verify(run_id)
        self._record_artifacts(run_id)
        current = self.state_for(run_id)
        if current.status.is_terminal:
            # Fail-closed artifact recording may have already stopped the run.
            return current
        if verification.passed:
            decision = ControlPolicy(self.budget).evaluate(current, now=self.clock.now())
            if decision.stop_reason is not None:
                self._stop(run_id, decision.stop_reason, decision.reason_code)
                return self.state_for(run_id)
            # A passed verification must stop through the control policy; never
            # fall through to a failure stop (or worse, no stop) silently.
            self._stop(
                run_id,
                StopReason.FAILURE,
                "ORCHESTRATOR_CONTROL_INCONSISTENT",
                summary="ORCHESTRATOR_CONTROL_INCONSISTENT: passed verification produced no stop",
            )
            return self.state_for(run_id)
        self._stop(
            run_id,
            StopReason.FAILURE,
            "MERGED_VERIFICATION_FAILED",
            summary=_bounded(f"MERGED_VERIFICATION_FAILED: {verification.summary}"),
        )
        return self.state_for(run_id)

    def state_for(self, run_id: RunId) -> RunState:
        events = self.store.events_for(run_id)
        if not events:
            msg = f"no persisted run: {run_id}"
            raise LookupError(msg)
        return replay(run_id, events)

    def _interleave(self, run_ids: dict[WorkerId, RunId]) -> tuple[bool, dict[WorkerId, RunState]]:
        """Drive workers round-robin one cycle at a time until all terminal.

        Returns the defense-in-depth budget flag and each worker's latest
        state. When the aggregate check trips, remaining workers are cancelled
        explicitly so no sibling runs unsupervised.
        """
        latest: dict[WorkerId, RunState] = {}
        pending = dict(run_ids)
        budget_tripped = False
        while pending:
            for worker in self.workers:
                worker_run_id = pending.get(worker.worker_id)
                if worker_run_id is None:
                    continue
                state = worker.runtime.step(worker_run_id)
                latest[worker.worker_id] = state
                if state.status.is_terminal or state.status is RunStatus.WAITING_FOR_APPROVAL:
                    del pending[worker.worker_id]
            aggregate = sum(state.cost_usd for state in latest.values())
            if not budget_tripped and aggregate >= self.budget.max_cost_usd:
                # Static shares sum to no more than the global limit, so this
                # can only trip on within-turn overshoot; cancel the remaining
                # workers explicitly rather than letting siblings run on.
                budget_tripped = True
                for worker in self.workers:
                    worker_run_id = pending.get(worker.worker_id)
                    if worker_run_id is None:
                        continue
                    latest[worker.worker_id] = worker.runtime.cancel(
                        worker_run_id,
                        summary="cancelled: orchestrated aggregate budget exhausted",
                    )
                    del pending[worker.worker_id]
        return budget_tripped, latest

    def _record_outcomes(
        self, run_id: RunId, latest: dict[WorkerId, RunState]
    ) -> dict[WorkerId, WorkerOutcome]:
        outcomes: dict[WorkerId, WorkerOutcome] = {}
        for worker in self.workers:
            state = latest[worker.worker_id]
            outcome = _worker_outcome(state.status)
            outcomes[worker.worker_id] = outcome
            factory = self._worker_stopped_factory(worker, outcome, state)
            self._persist(run_id, factory)
        return outcomes

    @staticmethod
    def _worker_stopped_factory(
        worker: WorkerBinding, outcome: WorkerOutcome, state: RunState
    ) -> EventFactory:
        return lambda event_id, rid, occurred_at, sequence: WorkerStopped(
            event_id=event_id,
            run_id=rid,
            occurred_at=occurred_at,
            sequence=sequence,
            worker_id=worker.worker_id,
            outcome=outcome,
            summary=f"worker run reached {state.status.value}",
        )

    def _verify(self, run_id: RunId) -> VerificationResult:
        verification = self.verifier.verify(self.state_for(run_id))
        # Boundary validation is intentional: adapters may violate port types.
        if not isinstance(verification, VerificationResult):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = f"verifier returned {type(verification).__name__}, expected VerificationResult"
            raise VerifierContractError(msg)
        if verification.passed:
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence: VerificationPassed(
                    event_id=event_id,
                    run_id=rid,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    summary=verification.summary,
                ),
            )
        else:
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence: VerificationFailed(
                    event_id=event_id,
                    run_id=rid,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    summary=verification.summary,
                    score=verification.score,
                ),
            )
        return verification

    def _record_artifacts(self, run_id: RunId) -> None:
        """Persist merged-workspace evidence after verification (fail-closed)."""
        if self.artifacts is None:
            return
        state = self.state_for(run_id)
        try:
            collected = self.artifacts.collect(state)
            validated = tuple(self._validate_artifact(item) for item in collected)
        except Exception as exc:
            self._stop(
                run_id,
                StopReason.FAILURE,
                "ARTIFACT_COLLECTION_FAILED",
                summary=f"artifact collection failed: {type(exc).__name__}: {exc}",
            )
            return
        known = {
            (fingerprint.kind, fingerprint.label, fingerprint.content)
            for fingerprint in state.recorded_artifacts
        }
        for artifact in validated:
            if (artifact.kind.value, artifact.label, artifact.content) in known:
                continue
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence, artifact=artifact: ArtifactRecorded(
                    event_id=event_id,
                    run_id=rid,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    kind=artifact.kind,
                    label=artifact.label,
                    content=artifact.content,
                ),
            )

    @staticmethod
    def _validate_artifact(artifact: object) -> RunArtifact:
        # Boundary validation is intentional: adapters may violate port return types.
        if not isinstance(artifact, RunArtifact):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = f"artifact collector returned {type(artifact).__name__}, expected RunArtifact"
            raise ArtifactContractError(msg)
        return artifact

    def _persist_plan(self, run_id: RunId, plan: str) -> None:
        self._persist(
            run_id,
            lambda event_id, rid, occurred_at, sequence: PlanCreated(
                event_id=event_id,
                run_id=rid,
                occurred_at=occurred_at,
                sequence=sequence,
                plan=plan,
            ),
        )

    def _stop(
        self,
        run_id: RunId,
        reason: StopReason,
        reason_code: str,
        *,
        summary: str | None = None,
    ) -> None:
        self._persist(
            run_id,
            lambda event_id, rid, occurred_at, sequence: RunStopped(
                event_id=event_id,
                run_id=rid,
                occurred_at=occurred_at,
                sequence=sequence,
                reason=reason,
                summary=summary or reason_code,
            ),
        )
        final = self.state_for(run_id)
        self._telemetry.emit_run_span(
            run_id,
            started_at=final.started_at or self.clock.now(),
            reason=reason,
            attributes={
                "loopforge.run.outcome": reason.value,
                "loopforge.run.iterations": final.iteration,
                "loopforge.run.cost_usd": final.cost_usd,
                "loopforge.run.workers": len(final.workers),
            },
        )

    def _persist(self, run_id: RunId, factory: EventFactory) -> None:
        expected_version = self.store.current_version(run_id)
        event = factory(
            EventId(f"evt_{uuid4().hex[:16]}"),
            run_id,
            self.clock.now(),
            expected_version + 1,
        )
        self.store.append(event, expected_version=expected_version)
        # Telemetry is projected only after the authoritative event is durable;
        # it never feeds back into orchestration decisions.
        self._telemetry.project_event(event)


def _worker_outcome(status: RunStatus) -> WorkerOutcome:
    if status is RunStatus.SUCCEEDED:
        return WorkerOutcome.SUCCEEDED
    if status is RunStatus.CANCELLED:
        return WorkerOutcome.CANCELLED
    if status is RunStatus.BUDGET_EXHAUSTED:
        return WorkerOutcome.BUDGET_EXHAUSTED
    return WorkerOutcome.FAILED


_WORKER_TEXT_BUDGET = 500


def _bounded(text: str) -> str:
    """Bound and sanitize text crossing adapter boundaries into durable events."""
    sanitized = "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in text)
    if len(sanitized) <= _WORKER_TEXT_BUDGET:
        return sanitized
    return sanitized[: _WORKER_TEXT_BUDGET - 3] + "..."
