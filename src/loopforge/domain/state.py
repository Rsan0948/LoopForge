from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ContextItemSnapshot
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    ActionRejected,
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    ArtifactRecorded,
    BudgetDebited,
    CircuitOpened,
    ContextAssembled,
    Event,
    ModelTurnRecorded,
    OperatorInstruction,
    PlanCreated,
    ReflectionRecorded,
    RetryScheduled,
    RunStarted,
    RunStopped,
    ToolExecutionStarted,
    ToolFailed,
    ToolSucceeded,
    VerificationFailed,
    VerificationPassed,
    WorkerMerged,
    WorkerSpawned,
    WorkerStopped,
)
from loopforge.domain.orchestration import MergeOutcome, WorkerOutcome, WorkerProjection
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import ToolMetadata
from loopforge.domain.types import BudgetLimit, RunId, RunStatus, StopReason


class InvalidTransitionError(RuntimeError):
    """Raised when an event is illegal for the current run state."""


@dataclass(frozen=True, slots=True)
class ToolFailureStreak:
    tool_name: str
    count: int


@dataclass(frozen=True, slots=True)
class ArtifactFingerprint:
    """Projected identity of one recorded evidence artifact.

    Content strings are shared references (already held by the decoded event
    stream), so this projection adds no payload copies — it exists so the
    runtime can skip byte-identical re-records after a crash/resume while
    never dropping fresh per-cycle evidence.
    """

    kind: str
    label: str
    content: str


@dataclass(frozen=True, slots=True)
class RunState:
    run_id: RunId
    status: RunStatus = RunStatus.CREATED
    objective: str = ""
    parent_run_id: RunId | None = None
    plan: str | None = None
    iteration: int = 0
    current_action_id: str | None = None
    current_proposal: ActionProposal | None = None
    current_tool_metadata: ToolMetadata | None = None
    current_attempt: int = 0
    current_idempotency_key: str | None = None
    retry_not_before: datetime | None = None
    execution_in_flight: bool = False
    last_observation: str | None = None
    last_tool_failure_class: ToolFailureClass | None = None
    last_verification: str | None = None
    last_verification_passed: bool | None = None
    last_verification_score: float | None = None
    best_verification_score: float | None = None
    consecutive_no_progress: int = 0
    last_reflection: str | None = None
    last_context_items: tuple[ContextItemSnapshot, ...] | None = None
    tool_failure_streaks: tuple[ToolFailureStreak, ...] = ()
    open_circuit_tools: tuple[str, ...] = ()
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    started_at: datetime | None = None
    last_occurred_at: datetime | None = None
    stop_reason: StopReason | None = None
    recorded_artifacts: tuple[ArtifactFingerprint, ...] = ()
    workers: tuple[WorkerProjection, ...] = ()
    # Operator-control projections (PACS-014): durably approved actions the
    # drive cycle may execute without a fresh approval request, the operator
    # steering ledger, and the most recent approval-denial reason.
    approved_action_ids: tuple[str, ...] = ()
    operator_instructions: tuple[str, ...] = ()
    last_approval_rejection: str | None = None
    version: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def failure_streak_for(self, tool_name: str) -> int:
        for item in self.tool_failure_streaks:
            if item.tool_name == tool_name:
                return item.count
        return 0


_ALLOWED_STATUS: dict[type[Event], set[RunStatus]] = {
    RunStarted: {RunStatus.CREATED},
    PlanCreated: {RunStatus.PLANNING, RunStatus.REFLECTING},
    ActionProposed: {RunStatus.READY},
    ActionAuthorized: {RunStatus.READY},
    ActionRejected: {RunStatus.READY},
    ToolExecutionStarted: {RunStatus.ACTING},
    ToolSucceeded: {RunStatus.ACTING},
    ToolFailed: {RunStatus.ACTING},
    RetryScheduled: {RunStatus.VERIFYING},
    CircuitOpened: {RunStatus.VERIFYING},
    VerificationPassed: {RunStatus.VERIFYING},
    VerificationFailed: {RunStatus.VERIFYING},
    ReflectionRecorded: {RunStatus.REFLECTING},
    ContextAssembled: {RunStatus.READY},
    ArtifactRecorded: {RunStatus.VERIFYING, RunStatus.REFLECTING},
    BudgetDebited: {
        RunStatus.PLANNING,
        RunStatus.READY,
        RunStatus.ACTING,
        RunStatus.VERIFYING,
        RunStatus.REFLECTING,
    },
    ModelTurnRecorded: {RunStatus.READY},
    ApprovalRequested: {RunStatus.READY},
    ApprovalGranted: {RunStatus.WAITING_FOR_APPROVAL},
    ApprovalRejected: {RunStatus.WAITING_FOR_APPROVAL},
    OperatorInstruction: {
        RunStatus.READY,
        RunStatus.REFLECTING,
        RunStatus.WAITING_FOR_APPROVAL,
    },
    WorkerSpawned: {RunStatus.READY, RunStatus.ACTING},
    WorkerStopped: {RunStatus.ACTING},
    WorkerMerged: {RunStatus.ACTING},
    RunStopped: {
        RunStatus.PLANNING,
        RunStatus.READY,
        RunStatus.ACTING,
        RunStatus.VERIFYING,
        RunStatus.REFLECTING,
        RunStatus.WAITING_FOR_APPROVAL,
    },
}


def _validate_transition(state: RunState, event: Event) -> None:
    if state.status.is_terminal:
        msg = f"terminal run cannot accept {type(event).__name__}"
        raise InvalidTransitionError(msg)
    allowed = _ALLOWED_STATUS[type(event)]
    if state.status not in allowed:
        msg_2 = f"{type(event).__name__} is invalid while run is {state.status.value}"
        raise InvalidTransitionError(msg_2)

    if isinstance(event, (ApprovalRequested, ApprovalGranted, ApprovalRejected)) and (
        state.current_action_id is None or str(event.action_id) != state.current_action_id
    ):
        msg_14 = "approval event does not match the pending action"
        raise InvalidTransitionError(msg_14)

    if isinstance(event, (ToolExecutionStarted, ToolSucceeded, ToolFailed, RetryScheduled)) and (
        state.current_action_id is None or str(event.action_id) != state.current_action_id
    ):
        msg_3 = "tool journal event does not match current action"
        raise InvalidTransitionError(msg_3)

    if isinstance(event, ToolExecutionStarted):
        expected_attempt = state.current_attempt + 1
        if event.attempt != expected_attempt:
            msg_4 = f"tool attempt {event.attempt} does not match expected {expected_attempt}"
            raise InvalidTransitionError(msg_4)
    elif isinstance(event, (ToolSucceeded, ToolFailed)):
        # Legacy schema-v1 streams may have an outcome directly after ActionAuthorized
        # without a ToolExecutionStarted journal event. Preserve replay compatibility.
        if state.current_attempt not in {0, event.attempt}:
            msg_7 = "tool outcome attempt does not match active attempt"
            raise InvalidTransitionError(msg_7)
    elif isinstance(event, RetryScheduled):
        active_attempt = max(1, state.current_attempt)
        if event.next_attempt != active_attempt + 1:
            msg_8 = "retry attempt is not the next journal attempt"
            raise InvalidTransitionError(msg_8)


def _update_streak(
    streaks: tuple[ToolFailureStreak, ...], tool_name: str, *, success: bool
) -> tuple[ToolFailureStreak, ...]:
    items = {item.tool_name: item.count for item in streaks}
    items[tool_name] = 0 if success else items.get(tool_name, 0) + 1
    return tuple(ToolFailureStreak(name, count) for name, count in sorted(items.items()))


def _verification_progress(
    state: RunState, *, summary: str, score: float | None
) -> tuple[float | None, int]:
    if score is not None:
        if state.best_verification_score is None or score > state.best_verification_score:
            return score, 0
        return state.best_verification_score, state.consecutive_no_progress + 1
    if state.last_verification is None or summary != state.last_verification:
        return state.best_verification_score, 0
    return state.best_verification_score, state.consecutive_no_progress + 1


def _update_worker(
    workers: tuple[WorkerProjection, ...],
    worker_id: str,
    *,
    outcome: WorkerOutcome | None = None,
    merge_outcome: MergeOutcome | None = None,
) -> tuple[WorkerProjection, ...]:
    """Roster bookkeeping: lifecycle events update exactly one known worker."""
    for worker in workers:
        if str(worker.worker_id) == worker_id:
            updated = worker
            if outcome is not None:
                if updated.outcome is not None:
                    msg_9 = f"worker {worker_id} already has a terminal outcome"
                    raise InvalidTransitionError(msg_9)
                updated = replace(updated, outcome=outcome)
            if merge_outcome is not None:
                if updated.merge_outcome is not None:
                    msg_10 = f"worker {worker_id} already has a merge outcome"
                    raise InvalidTransitionError(msg_10)
                if updated.outcome is not WorkerOutcome.SUCCEEDED:
                    msg_11 = f"only a succeeded worker may merge: {worker_id}"
                    raise InvalidTransitionError(msg_11)
                updated = replace(updated, merge_outcome=merge_outcome)
            return tuple(updated if str(item.worker_id) == worker_id else item for item in workers)
    msg_12 = f"unknown worker: {worker_id}"
    raise InvalidTransitionError(msg_12)


def _workers_resolved(workers: tuple[WorkerProjection, ...]) -> bool:
    """Every worker stopped and every succeeded worker reconciled."""
    return bool(workers) and all(
        worker.outcome is not None
        and (worker.outcome is not WorkerOutcome.SUCCEEDED or worker.merge_outcome is not None)
        for worker in workers
    )


def reduce_event(state: RunState, event: Event) -> RunState:  # noqa: PLR0911, PLR0912
    """Project one immutable event into a new immutable RunState.

    The flat match dispatch is deliberate: each event projection stays a single
    auditable case rather than being scattered across handler indirection.
    """
    _validate_transition(state, event)
    base = replace(
        state,
        version=state.version + 1,
        last_occurred_at=event.occurred_at,
    )

    match event:
        case RunStarted(objective=objective, parent_run_id=parent_run_id):
            return replace(
                base,
                objective=objective,
                parent_run_id=parent_run_id,
                status=RunStatus.PLANNING,
                started_at=event.occurred_at,
            )
        case PlanCreated(plan=plan):
            return replace(base, plan=plan, status=RunStatus.READY)
        case ActionProposed(proposal=proposal):
            return replace(
                base,
                current_action_id=str(proposal.action_id),
                current_proposal=proposal,
                current_tool_metadata=None,
                current_attempt=0,
                current_idempotency_key=None,
                retry_not_before=None,
                execution_in_flight=False,
            )
        case ActionAuthorized(proposal=proposal, tool_metadata=metadata):
            return replace(
                base,
                current_action_id=str(proposal.action_id),
                current_proposal=proposal,
                current_tool_metadata=metadata,
                status=RunStatus.ACTING,
                iteration=base.iteration + 1,
            )
        case ActionRejected():
            return replace(
                base,
                current_action_id=None,
                current_proposal=None,
                current_tool_metadata=None,
                current_attempt=0,
                current_idempotency_key=None,
                retry_not_before=None,
                execution_in_flight=False,
                # A grant dies with its action: an approved action that is
                # permanently rejected must not leave its grant behind. Grants
                # are keyed by action id, and action ids are not unique across
                # adapter rebuilds (e.g. the Ollama adapter restarts its turn
                # counter after a process restart), so a lingering grant would
                # silently authorize a later gated proposal that reuses the id.
                approved_action_ids=tuple(
                    action_id
                    for action_id in base.approved_action_ids
                    if action_id != base.current_action_id
                ),
                status=RunStatus.READY,
            )
        case ToolExecutionStarted(
            attempt=attempt,
            idempotency_key=idempotency_key,
        ):
            return replace(
                base,
                current_attempt=attempt,
                current_idempotency_key=idempotency_key,
                retry_not_before=None,
                execution_in_flight=True,
            )
        case ToolSucceeded(observation=observation, attempt=attempt):
            tool_name = base.current_tool_metadata.name if base.current_tool_metadata else "unknown"
            return replace(
                base,
                last_observation=observation,
                last_tool_failure_class=None,
                current_attempt=attempt,
                execution_in_flight=False,
                tool_failure_streaks=_update_streak(
                    base.tool_failure_streaks, tool_name, success=True
                ),
                # A grant is spent by the successful execution of its action.
                # Without this, a re-planned cycle would see READY + stale
                # proposal + lingering grant and re-execute the same approved
                # action — durably poisoning the stream with an attempt-1
                # duplicate (found by the PACS-014 live round-trip).
                approved_action_ids=tuple(
                    action_id
                    for action_id in base.approved_action_ids
                    if action_id != base.current_action_id
                ),
                status=RunStatus.VERIFYING,
            )
        case ToolFailed(error_message=message, failure_class=failure_class, attempt=attempt):
            tool_name = base.current_tool_metadata.name if base.current_tool_metadata else "unknown"
            return replace(
                base,
                last_observation=message,
                last_tool_failure_class=failure_class,
                current_attempt=attempt,
                execution_in_flight=False,
                tool_failure_streaks=_update_streak(
                    base.tool_failure_streaks, tool_name, success=False
                ),
                # A grant is spent by the execution of its action, whatever the
                # outcome. A failed-but-granted action that re-plans back to
                # READY must not keep its grant: the next cycle would
                # re-execute the stale approved proposal at attempt 1 while the
                # journal expects attempt 2 — the same durable stream poison as
                # the successful-outcome case above. Retries never consult the
                # grant (the retry path re-executes the in-flight action
                # directly), so consuming here cannot starve a bounded retry.
                approved_action_ids=tuple(
                    action_id
                    for action_id in base.approved_action_ids
                    if action_id != base.current_action_id
                ),
                status=RunStatus.VERIFYING,
            )
        case RetryScheduled(next_attempt=next_attempt, delay_seconds=delay_seconds):
            return replace(
                base,
                current_attempt=next_attempt - 1,
                retry_not_before=event.occurred_at + timedelta(seconds=delay_seconds),
                execution_in_flight=False,
                status=RunStatus.ACTING,
            )
        case CircuitOpened(tool_name=tool_name):
            return replace(
                base,
                open_circuit_tools=tuple(sorted(set(base.open_circuit_tools) | {tool_name})),
            )
        case VerificationPassed(summary=summary):
            return replace(
                base,
                last_verification=summary,
                last_verification_passed=True,
                last_verification_score=1.0,
                best_verification_score=max(base.best_verification_score or 0.0, 1.0),
                consecutive_no_progress=0,
                status=RunStatus.VERIFYING,
            )
        case VerificationFailed(summary=summary, score=score):
            best_score, no_progress = _verification_progress(base, summary=summary, score=score)
            return replace(
                base,
                last_verification=summary,
                last_verification_passed=False,
                last_verification_score=score,
                best_verification_score=best_score,
                consecutive_no_progress=no_progress,
                status=RunStatus.REFLECTING,
            )
        case ReflectionRecorded(reflection=reflection):
            return replace(base, last_reflection=reflection, status=RunStatus.REFLECTING)
        case ContextAssembled(context_items=context_items):
            return replace(base, last_context_items=context_items)
        case ArtifactRecorded(kind=kind, label=label, content=content):
            # Evidence-only durability record: the exact artifact stays in the
            # authoritative event stream. A (kind, label, content) fingerprint
            # is projected so the runtime can keep evidence at-most-once
            # across crash/resume without dropping fresh per-cycle evidence.
            fingerprint = ArtifactFingerprint(kind=kind.value, label=label, content=content)
            return replace(base, recorded_artifacts=(*base.recorded_artifacts, fingerprint))
        case BudgetDebited(usage=usage):
            return replace(
                base,
                cost_usd=base.cost_usd + usage.cost_usd,
                input_tokens=base.input_tokens + usage.input_tokens,
                output_tokens=base.output_tokens + usage.output_tokens,
                cached_input_tokens=base.cached_input_tokens + usage.cached_input_tokens,
            )
        case ModelTurnRecorded():
            # Evidence-only model-identity record (PACS-015): the provenance
            # graph attributes the turn's action to this provider/model. No
            # control-state effect; the payload stays in the event stream.
            return base
        case ApprovalRequested():
            return replace(base, status=RunStatus.WAITING_FOR_APPROVAL)
        case ApprovalGranted(action_id=action_id):
            return replace(
                base,
                status=RunStatus.READY,
                approved_action_ids=(*base.approved_action_ids, str(action_id)),
            )
        case ApprovalRejected(reason=reason):
            return replace(
                base,
                current_action_id=None,
                current_proposal=None,
                current_tool_metadata=None,
                current_attempt=0,
                current_idempotency_key=None,
                retry_not_before=None,
                execution_in_flight=False,
                status=RunStatus.READY,
                last_approval_rejection=reason,
            )
        case OperatorInstruction(instruction=instruction, amends_objective=amends):
            return replace(
                base,
                objective=instruction if amends else base.objective,
                operator_instructions=(*base.operator_instructions, instruction),
            )
        case RunStopped(reason=reason):
            return replace(
                base,
                status=_TERMINAL_STATUS_BY_REASON[reason],
                stop_reason=reason,
            )
        case WorkerSpawned(
            worker_id=worker_id,
            worker_run_id=worker_run_id,
            workspace_id=workspace_id,
            budget_share_cost_usd=share,
        ):
            if any(str(item.worker_id) == str(worker_id) for item in base.workers):
                msg_13 = f"worker {worker_id} is already on the roster"
                raise InvalidTransitionError(msg_13)
            projection = WorkerProjection(
                worker_id=worker_id,
                worker_run_id=worker_run_id,
                workspace_id=workspace_id,
                budget_share_cost_usd=share,
            )
            return replace(base, workers=(*base.workers, projection), status=RunStatus.ACTING)
        case WorkerStopped(worker_id=worker_id, outcome=outcome):
            return replace(
                base,
                workers=_update_worker(base.workers, str(worker_id), outcome=outcome),
            )
        case WorkerMerged(worker_id=worker_id, outcome=outcome):
            workers = _update_worker(base.workers, str(worker_id), merge_outcome=outcome)
            status = RunStatus.VERIFYING if _workers_resolved(workers) else base.status
            return replace(base, workers=workers, status=status)


def replay(run_id: RunId, events: tuple[Event, ...]) -> RunState:
    state = RunState(run_id=run_id)
    for expected_sequence, event in enumerate(events, start=1):
        if event.run_id != run_id:
            msg_5 = f"event {event.event_id} belongs to run {event.run_id}, expected {run_id}"
            raise ValueError(msg_5)
        if event.sequence != expected_sequence:
            msg_6 = f"expected sequence {expected_sequence}, got {event.sequence}"
            raise ValueError(msg_6)
        state = reduce_event(state, event)
    return state


def budget_exceeded(state: RunState, limit: BudgetLimit) -> bool:
    if state.cost_usd >= limit.max_cost_usd or state.iteration >= limit.max_iterations:
        return True
    return limit.max_total_tokens is not None and state.total_tokens >= limit.max_total_tokens


_TERMINAL_STATUS_BY_REASON: dict[StopReason, RunStatus] = {
    StopReason.SUCCESS_VERIFIED: RunStatus.SUCCEEDED,
    StopReason.FAILURE: RunStatus.FAILED,
    StopReason.STALLED: RunStatus.STALLED,
    StopReason.BUDGET_EXHAUSTED: RunStatus.BUDGET_EXHAUSTED,
    StopReason.MAX_ITERATIONS: RunStatus.FAILED,
    StopReason.CANCELLED: RunStatus.CANCELLED,
}

# Deterministic post-status for events whose reducer arm always lands in one
# status. Events absent from this table leave the run status unchanged
# (BudgetDebited, ArtifactRecorded, CircuitOpened, OperatorInstruction,
# ModelTurnRecorded, WorkerStopped, WorkerMerged); RunStopped is reason-dependent
# and handled by
# status_after_event. The reducer above remains the authority — store adapters
# use this projection only to maintain their runs index, and a conformance
# suite pins it equal to full replay.
_STATUS_AFTER_EVENT: dict[type[Event], RunStatus] = {
    RunStarted: RunStatus.PLANNING,
    PlanCreated: RunStatus.READY,
    ActionProposed: RunStatus.READY,
    ActionAuthorized: RunStatus.ACTING,
    ActionRejected: RunStatus.READY,
    ToolExecutionStarted: RunStatus.ACTING,
    ToolSucceeded: RunStatus.VERIFYING,
    ToolFailed: RunStatus.VERIFYING,
    RetryScheduled: RunStatus.ACTING,
    VerificationPassed: RunStatus.VERIFYING,
    VerificationFailed: RunStatus.REFLECTING,
    ReflectionRecorded: RunStatus.REFLECTING,
    ContextAssembled: RunStatus.READY,
    ApprovalRequested: RunStatus.WAITING_FOR_APPROVAL,
    ApprovalGranted: RunStatus.READY,
    ApprovalRejected: RunStatus.READY,
    WorkerSpawned: RunStatus.ACTING,
}


def status_after_event(event: Event) -> RunStatus | None:
    """The run status after ``event``, or ``None`` when the status is unchanged.

    Used by durable store adapters to maintain their runs-index projection
    transactionally with each append. Mirrors the reducer arms exactly for
    single-runtime streams; the one known divergence is a final
    ``WorkerMerged`` that resolves an orchestrated roster (replay: VERIFYING,
    index: ACTING until the next event), which is transient and self-heals on
    the following verification event.
    """
    if isinstance(event, RunStopped):
        return _TERMINAL_STATUS_BY_REASON[event.reason]
    return _STATUS_AFTER_EVENT.get(type(event))


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Run-metadata projection for the runs index (StateStorePort.list_runs).

    A listing summary, never authority: every field derives from the
    authoritative event stream and is recomputable by replay at any time.
    """

    run_id: RunId
    objective: str
    status: RunStatus
    started_at: datetime
    last_occurred_at: datetime
    cost_usd: float
    stop_reason: StopReason | None


def summarize_run(run_id: RunId, events: tuple[Event, ...]) -> RunRecord:
    """Project one run's metadata record from its authoritative stream by replay."""
    state = replay(run_id, events)
    if state.started_at is None or state.last_occurred_at is None:
        msg = f"run {run_id} stream is missing lifecycle timestamps"
        raise ValueError(msg)
    return RunRecord(
        run_id=run_id,
        objective=state.objective,
        status=state.status,
        started_at=state.started_at,
        last_occurred_at=state.last_occurred_at,
        cost_usd=state.cost_usd,
        stop_reason=state.stop_reason,
    )
