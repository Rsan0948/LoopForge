from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import ArtifactKind
from loopforge.domain.context import ContextItemSnapshot, ContextSource
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
from loopforge.domain.orchestration import MergeOutcome, WorkerOutcome
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.security import TrustClass
from loopforge.domain.state import (
    InvalidTransitionError,
    RunState,
    ToolFailureStreak,
    budget_exceeded,
    reduce_event,
    replay,
)
from loopforge.domain.tooling import (
    ApprovalClass,
    DataSensitivity,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import (
    ActionId,
    BudgetLimit,
    ContextItemId,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    RunStatus,
    StopReason,
    UsageDelta,
    WorkerId,
    WorkspaceId,
)

NOW = datetime(2026, 8, 22, tzinfo=UTC)
RUN = RunId("state-run")


def _metadata(name: str = "inspect") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _proposal(action_id: str = "a1", tool_name: str = "inspect") -> ActionProposal:
    return ActionProposal(ActionId(action_id), tool_name, {})


def _event_id(sequence: int) -> EventId:
    return EventId(f"e{sequence}")


def _context_assembled(sequence: int) -> ContextAssembled:
    return ContextAssembled(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        context_items=(
            ContextItemSnapshot(
                item_id=ContextItemId(f"{RUN}:objective"),
                content="repair",
                trust=TrustClass.AUTHORIZED_HUMAN,
                source=ContextSource(
                    origin=TrustClass.AUTHORIZED_HUMAN,
                    reference=f"run:{RUN}:objective",
                ),
                sensitivity=DataSensitivity.INTERNAL,
                created_at=NOW,
            ),
        ),
    )


def _started(sequence: int, *, run_id: RunId = RUN) -> RunStarted:
    return RunStarted(
        event_id=_event_id(sequence),
        run_id=run_id,
        occurred_at=NOW,
        sequence=sequence,
        objective="repair",
    )


def _planned(sequence: int, *, plan: str = "inspect repo") -> PlanCreated:
    return PlanCreated(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        plan=plan,
    )


def _proposed(sequence: int, *, proposal: ActionProposal | None = None) -> ActionProposed:
    if proposal is None:
        proposal = _proposal()
    return ActionProposed(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        proposal=proposal,
    )


def _authorized(
    sequence: int,
    *,
    proposal: ActionProposal | None = None,
    metadata: ToolMetadata | None = None,
) -> ActionAuthorized:
    if proposal is None:
        proposal = _proposal()
    if metadata is None:
        metadata = _metadata(proposal.tool_name)
    return ActionAuthorized(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        proposal=proposal,
        tool_metadata=metadata,
    )


def _rejected(sequence: int, *, proposal: ActionProposal | None = None) -> ActionRejected:
    if proposal is None:
        proposal = _proposal()
    return ActionRejected(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        proposal=proposal,
        reason_code="PERMISSION_DENIED",
    )


def _tool_started(
    sequence: int,
    *,
    action_id: str = "a1",
    attempt: int = 1,
    idempotency_key: str | None = None,
) -> ToolExecutionStarted:
    return ToolExecutionStarted(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        action_id=ActionId(action_id),
        attempt=attempt,
        idempotency_key=idempotency_key,
    )


def _tool_succeeded(
    sequence: int,
    *,
    action_id: str = "a1",
    observation: str = "ok",
    attempt: int = 1,
) -> ToolSucceeded:
    return ToolSucceeded(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        action_id=ActionId(action_id),
        observation=observation,
        attempt=attempt,
    )


def _tool_failed(
    sequence: int,
    *,
    action_id: str = "a1",
    attempt: int = 1,
    failure_class: ToolFailureClass = ToolFailureClass.TRANSIENT,
    error_message: str = "boom",
) -> ToolFailed:
    return ToolFailed(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        action_id=ActionId(action_id),
        error_code="E_BOOM",
        error_message=error_message,
        failure_class=failure_class,
        attempt=attempt,
    )


def _retry_scheduled(
    sequence: int,
    *,
    action_id: str = "a1",
    next_attempt: int = 2,
    delay_seconds: float = 0.5,
) -> RetryScheduled:
    return RetryScheduled(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        action_id=ActionId(action_id),
        next_attempt=next_attempt,
        delay_seconds=delay_seconds,
        reason_code="RETRY_TRANSIENT_FAILURE",
    )


def _circuit_opened(sequence: int, *, tool_name: str = "inspect") -> CircuitOpened:
    return CircuitOpened(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        tool_name=tool_name,
        reason_code="CIRCUIT_FAILURE_THRESHOLD",
    )


def _verification_passed(sequence: int, *, summary: str = "all checks pass") -> VerificationPassed:
    return VerificationPassed(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        summary=summary,
    )


def _verification_failed(
    sequence: int,
    *,
    summary: str = "tests failing",
    score: float | None = None,
) -> VerificationFailed:
    return VerificationFailed(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        summary=summary,
        score=score,
    )


def _reflection(sequence: int, *, reflection: str = "try a smaller diff") -> ReflectionRecorded:
    return ReflectionRecorded(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        reflection=reflection,
    )


def _budget(sequence: int, *, usage: UsageDelta | None = None) -> BudgetDebited:
    if usage is None:
        usage = UsageDelta(cost_usd=0.25, input_tokens=10, output_tokens=5, cached_input_tokens=2)
    return BudgetDebited(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        usage=usage,
    )


def _approval_requested(sequence: int, *, action_id: str = "a1") -> ApprovalRequested:
    return ApprovalRequested(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        action_id=ActionId(action_id),
        reason="POLICY_DEPENDENT",
    )


def _approval_granted(sequence: int, *, action_id: str = "a1") -> ApprovalGranted:
    return ApprovalGranted(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        action_id=ActionId(action_id),
    )


def _operator_instruction(
    sequence: int,
    *,
    instruction: str = "focus on the failing test only",
    amends_objective: bool = False,
) -> OperatorInstruction:
    return OperatorInstruction(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        instruction=instruction,
        amends_objective=amends_objective,
    )


def _stopped(sequence: int, *, reason: StopReason = StopReason.SUCCESS_VERIFIED) -> RunStopped:
    return RunStopped(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        reason=reason,
        summary="done",
    )


def _state_at(stage: str) -> RunState:
    if stage == "created":
        return RunState(run_id=RUN)
    chains: dict[str, tuple[Event, ...]] = {
        "planning": (_started(1),),
        "ready": (_started(1), _planned(2)),
        "waiting": (_started(1), _planned(2), _proposed(3), _approval_requested(4)),
        "acting": (_started(1), _planned(2), _proposed(3), _authorized(4)),
        "acting_in_flight": (
            _started(1),
            _planned(2),
            _proposed(3),
            _authorized(4),
            _tool_started(5),
        ),
        "verifying": (
            _started(1),
            _planned(2),
            _proposed(3),
            _authorized(4),
            _tool_started(5),
            _tool_failed(6),
        ),
        "reflecting": (
            _started(1),
            _planned(2),
            _proposed(3),
            _authorized(4),
            _tool_started(5),
            _tool_failed(6),
            _verification_failed(7),
        ),
    }
    return replay(RUN, chains[stage])


def _apply_outcome(state: RunState, tool_name: str, *, success: bool) -> RunState:
    """Apply one tool outcome while carrying only the failure streaks forward."""
    acting = replace(
        RunState(run_id=state.run_id),
        status=RunStatus.ACTING,
        current_action_id="a1",
        current_tool_metadata=_metadata(tool_name),
        tool_failure_streaks=state.tool_failure_streaks,
    )
    event: Event = _tool_succeeded(1, observation="ok") if success else _tool_failed(1)
    return reduce_event(acting, event)


def _apply_verification_failure(state: RunState, *, summary: str, score: float | None) -> RunState:
    """Apply one VerificationFailed while carrying only verification progress forward."""
    verifying = replace(
        RunState(run_id=state.run_id),
        status=RunStatus.VERIFYING,
        last_verification=state.last_verification,
        best_verification_score=state.best_verification_score,
        consecutive_no_progress=state.consecutive_no_progress,
    )
    return reduce_event(verifying, _verification_failed(1, summary=summary, score=score))


# --- projections: run lifecycle ---


def test_fresh_run_state_defaults() -> None:
    state = RunState(run_id=RUN)
    assert state.status is RunStatus.CREATED
    assert state.version == 0
    assert state.iteration == 0
    assert state.plan is None
    assert state.started_at is None
    assert state.last_occurred_at is None
    assert state.tool_failure_streaks == ()
    assert state.open_circuit_tools == ()


def test_run_started_enters_planning_and_records_start_time() -> None:
    state = reduce_event(RunState(run_id=RUN), _started(1))
    assert state.status is RunStatus.PLANNING
    assert state.objective == "repair"
    assert state.started_at == NOW
    assert state.last_occurred_at == NOW
    assert state.version == 1


def test_reduce_event_returns_new_state_and_bumps_version() -> None:
    state = _state_at("ready")
    later = datetime(2026, 8, 23, tzinfo=UTC)
    event = ActionProposed(
        event_id=EventId("e3"),
        run_id=RUN,
        occurred_at=later,
        sequence=3,
        proposal=_proposal(),
    )

    next_state = reduce_event(state, event)

    assert next_state is not state
    assert next_state.version == state.version + 1
    assert next_state.last_occurred_at == later
    assert state.status is RunStatus.READY
    assert state.version == 2


def test_plan_created_records_plan_and_enters_ready() -> None:
    state = reduce_event(_state_at("planning"), _planned(2, plan="patch auth"))
    assert state.status is RunStatus.READY
    assert state.plan == "patch auth"


def test_action_proposed_replaces_current_action_context() -> None:
    dirty = replace(
        _state_at("ready"),
        current_action_id="old",
        current_proposal=_proposal("old"),
        current_tool_metadata=_metadata(),
        current_attempt=3,
        current_idempotency_key="old-key",
        retry_not_before=NOW,
        execution_in_flight=True,
    )
    proposal = _proposal("a9")

    state = reduce_event(dirty, _proposed(3, proposal=proposal))

    assert state.status is RunStatus.READY
    assert state.current_action_id == "a9"
    assert state.current_proposal == proposal
    assert state.current_tool_metadata is None
    assert state.current_attempt == 0
    assert state.current_idempotency_key is None
    assert state.retry_not_before is None
    assert not state.execution_in_flight


def test_action_authorized_enters_acting_with_tool_contract() -> None:
    ready = _state_at("ready")
    proposal = _proposal()
    metadata = _metadata()
    proposed = reduce_event(ready, _proposed(3, proposal=proposal))

    state = reduce_event(proposed, _authorized(4, proposal=proposal, metadata=metadata))

    assert state.status is RunStatus.ACTING
    assert state.iteration == ready.iteration + 1
    assert state.current_action_id == "a1"
    assert state.current_proposal == proposal
    assert state.current_tool_metadata == metadata


def test_action_rejected_clears_context_and_stays_ready() -> None:
    proposed = reduce_event(_state_at("ready"), _proposed(3))

    state = reduce_event(proposed, _rejected(4))

    assert state.status is RunStatus.READY
    assert state.current_action_id is None
    assert state.current_proposal is None
    assert state.current_tool_metadata is None
    assert state.current_attempt == 0
    assert state.current_idempotency_key is None
    assert state.retry_not_before is None
    assert not state.execution_in_flight


# --- projections: tool journal ---


def test_tool_execution_started_journals_attempt_and_idempotency_key() -> None:
    state = reduce_event(
        _state_at("acting"),
        _tool_started(5, attempt=1, idempotency_key="loopforge:r:a1"),
    )
    assert state.current_attempt == 1
    assert state.current_idempotency_key == "loopforge:r:a1"
    assert state.retry_not_before is None
    assert state.execution_in_flight


def test_tool_succeeded_records_observation_and_enters_verifying() -> None:
    state = reduce_event(
        _state_at("acting_in_flight"),
        _tool_succeeded(6, observation="all tests pass", attempt=1),
    )
    assert state.status is RunStatus.VERIFYING
    assert state.last_observation == "all tests pass"
    assert state.last_tool_failure_class is None
    assert state.current_attempt == 1
    assert not state.execution_in_flight
    assert state.tool_failure_streaks == (ToolFailureStreak("inspect", 0),)


def test_tool_succeeded_without_metadata_falls_back_to_unknown_tool() -> None:
    acting = replace(_state_at("acting"), current_tool_metadata=None)

    state = reduce_event(acting, _tool_succeeded(5, observation="ok"))

    assert state.tool_failure_streaks == (ToolFailureStreak("unknown", 0),)


def test_tool_failed_records_failure_and_enters_verifying() -> None:
    state = reduce_event(
        _state_at("acting_in_flight"),
        _tool_failed(
            6,
            attempt=1,
            failure_class=ToolFailureClass.PERMANENT,
            error_message="command not found",
        ),
    )
    assert state.status is RunStatus.VERIFYING
    assert state.last_observation == "command not found"
    assert state.last_tool_failure_class is ToolFailureClass.PERMANENT
    assert state.current_attempt == 1
    assert not state.execution_in_flight
    assert state.tool_failure_streaks == (ToolFailureStreak("inspect", 1),)


def test_tool_failed_without_metadata_falls_back_to_unknown_tool() -> None:
    acting = replace(_state_at("acting"), current_tool_metadata=None)

    state = reduce_event(acting, _tool_failed(5))

    assert state.tool_failure_streaks == (ToolFailureStreak("unknown", 1),)


def test_retry_scheduled_returns_to_acting_with_backoff() -> None:
    state = reduce_event(
        _state_at("verifying"),
        _retry_scheduled(7, next_attempt=2, delay_seconds=0.5),
    )
    assert state.status is RunStatus.ACTING
    assert state.current_attempt == 1
    assert state.retry_not_before == NOW + timedelta(seconds=0.5)
    assert not state.execution_in_flight


def test_failed_attempt_can_retry_and_succeed() -> None:
    state = _state_at("verifying")
    state = reduce_event(state, _retry_scheduled(7, next_attempt=2, delay_seconds=0.5))
    state = reduce_event(state, _tool_started(8, attempt=2, idempotency_key="k2"))

    assert state.current_attempt == 2
    assert state.current_idempotency_key == "k2"
    assert state.retry_not_before is None
    assert state.execution_in_flight

    state = reduce_event(state, _tool_succeeded(9, observation="ok", attempt=2))

    assert state.status is RunStatus.VERIFYING
    assert state.last_observation == "ok"
    assert state.last_tool_failure_class is None
    assert state.failure_streak_for("inspect") == 0
    assert state.tool_failure_streaks == (ToolFailureStreak("inspect", 0),)


def test_circuit_opened_marks_tools_sorted_and_deduplicated() -> None:
    state = reduce_event(_state_at("verifying"), _circuit_opened(7, tool_name="inspect"))
    assert state.status is RunStatus.VERIFYING
    assert state.open_circuit_tools == ("inspect",)

    state = reduce_event(state, _circuit_opened(8, tool_name="beta"))
    state = reduce_event(state, _circuit_opened(9, tool_name="inspect"))

    assert state.open_circuit_tools == ("beta", "inspect")


# --- projections: verification and reflection ---


def test_verification_passed_records_perfect_score() -> None:
    state = reduce_event(
        _state_at("verifying"),
        _verification_passed(7, summary="all checks pass"),
    )
    assert state.status is RunStatus.VERIFYING
    assert state.last_verification == "all checks pass"
    assert state.last_verification_passed is True
    assert state.last_verification_score == 1.0
    assert state.best_verification_score == 1.0
    assert state.consecutive_no_progress == 0


def test_verification_passed_raises_partial_best_score_to_one() -> None:
    verifying = replace(_state_at("verifying"), best_verification_score=0.8)

    state = reduce_event(verifying, _verification_passed(7))

    assert state.best_verification_score == 1.0


def test_verification_failed_with_first_score_sets_best() -> None:
    state = reduce_event(
        _state_at("verifying"),
        _verification_failed(7, summary="2 tests fail", score=0.5),
    )
    assert state.status is RunStatus.REFLECTING
    assert state.last_verification == "2 tests fail"
    assert state.last_verification_passed is False
    assert state.last_verification_score == 0.5
    assert state.best_verification_score == 0.5
    assert state.consecutive_no_progress == 0


def test_verification_failed_with_improved_score_updates_best() -> None:
    verifying = replace(
        _state_at("verifying"),
        best_verification_score=0.5,
        consecutive_no_progress=3,
    )

    state = reduce_event(verifying, _verification_failed(7, summary="1 test fails", score=0.7))

    assert state.best_verification_score == 0.7
    assert state.consecutive_no_progress == 0


def test_verification_failed_without_improvement_counts_no_progress() -> None:
    verifying = replace(
        _state_at("verifying"),
        best_verification_score=0.5,
        consecutive_no_progress=2,
    )

    state = reduce_event(verifying, _verification_failed(7, summary="same", score=0.5))

    assert state.best_verification_score == 0.5
    assert state.consecutive_no_progress == 3


def test_verification_failed_without_score_and_no_prior_summary_resets() -> None:
    state = reduce_event(_state_at("verifying"), _verification_failed(7, summary="crash"))

    assert state.best_verification_score is None
    assert state.consecutive_no_progress == 0


def test_verification_failed_without_score_and_new_summary_resets() -> None:
    verifying = replace(
        _state_at("verifying"),
        last_verification="old summary",
        consecutive_no_progress=4,
    )

    state = reduce_event(verifying, _verification_failed(7, summary="new summary"))

    assert state.consecutive_no_progress == 0


def test_verification_failed_without_score_and_same_summary_counts_no_progress() -> None:
    verifying = replace(
        _state_at("verifying"),
        last_verification="same summary",
        consecutive_no_progress=4,
    )

    state = reduce_event(verifying, _verification_failed(7, summary="same summary"))

    assert state.consecutive_no_progress == 5


def test_reflection_recorded_stays_reflecting() -> None:
    state = reduce_event(
        _state_at("reflecting"),
        _reflection(8, reflection="split the change"),
    )
    assert state.status is RunStatus.REFLECTING
    assert state.last_reflection == "split the change"


def test_reflecting_run_can_replan_back_to_ready() -> None:
    state = reduce_event(_state_at("reflecting"), _planned(8, plan="smaller diff"))
    assert state.status is RunStatus.READY
    assert state.plan == "smaller diff"


def _artifact_recorded(sequence: int) -> ArtifactRecorded:
    return ArtifactRecorded(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        kind=ArtifactKind.WORKSPACE_SNAPSHOT,
        label="workspace:fixture",
        content="workspace_id=fixture\n\ndiff --git a/adder.py b/adder.py",
    )


def test_artifact_recorded_is_evidence_only_while_verifying() -> None:
    before = _state_at("verifying")

    state = reduce_event(before, _artifact_recorded(7))

    assert state.status is RunStatus.VERIFYING
    assert state.version == before.version + 1
    assert state.last_verification == before.last_verification
    assert state.last_observation == before.last_observation
    assert state.consecutive_no_progress == before.consecutive_no_progress


def test_artifact_recorded_is_allowed_while_reflecting() -> None:
    state = reduce_event(_state_at("reflecting"), _artifact_recorded(9))

    assert state.status is RunStatus.REFLECTING


def test_artifact_recorded_is_rejected_outside_verification() -> None:
    with pytest.raises(
        InvalidTransitionError, match="ArtifactRecorded is invalid while run is ready"
    ):
        reduce_event(_state_at("ready"), _artifact_recorded(3))


def test_context_assembled_projects_last_context_items_and_stays_ready() -> None:
    event = _context_assembled(3)

    state = reduce_event(_state_at("ready"), event)

    assert state.status is RunStatus.READY
    assert state.last_context_items == event.context_items


def test_context_assembled_provenance_survives_replay() -> None:
    events: tuple[Event, ...] = (_started(1), _planned(2), _context_assembled(3))

    state = replay(RUN, events)

    items = state.last_context_items
    assert items is not None
    assert items[0].trust is TrustClass.AUTHORIZED_HUMAN
    assert items[0].source.origin is TrustClass.AUTHORIZED_HUMAN
    assert items[0].source.reference == f"run:{RUN}:objective"


# --- projections: budget and approval ---


def test_budget_debited_accumulates_usage() -> None:
    usage = UsageDelta(cost_usd=0.25, input_tokens=10, output_tokens=5, cached_input_tokens=2)
    state = reduce_event(_state_at("ready"), _budget(3, usage=usage))
    state = reduce_event(state, _budget(4, usage=usage))

    assert state.cost_usd == 0.5
    assert state.input_tokens == 20
    assert state.output_tokens == 10
    assert state.cached_input_tokens == 4
    assert state.total_tokens == 30
    assert state.status is RunStatus.READY


def test_approval_request_and_grant_round_trip() -> None:
    state = replay(RUN, (_started(1), _planned(2), _proposed(3)))
    state = reduce_event(state, _approval_requested(4))
    assert state.status is RunStatus.WAITING_FOR_APPROVAL

    state = reduce_event(state, _approval_granted(5))
    assert state.status is RunStatus.READY
    assert state.approved_action_ids == ("a1",)
    # The approved proposal stays pending for the next drive cycle.
    assert state.current_action_id == "a1"
    assert state.current_proposal is not None


def test_approval_events_require_a_matching_pending_action() -> None:
    ready = _state_at("ready")
    with pytest.raises(InvalidTransitionError, match="approval event does not match"):
        reduce_event(ready, _approval_requested(3))

    waiting = _state_at("waiting")
    with pytest.raises(InvalidTransitionError, match="approval event does not match"):
        reduce_event(waiting, _approval_granted(4, action_id="other"))


def test_approval_rejection_clears_the_pending_action() -> None:
    waiting = _state_at("waiting")

    state = reduce_event(
        waiting,
        ApprovalRejected(
            event_id=_event_id(5),
            run_id=RUN,
            occurred_at=NOW,
            sequence=5,
            action_id=ActionId("a1"),
            reason="operator denied the write",
        ),
    )

    assert state.status is RunStatus.READY
    assert state.current_action_id is None
    assert state.current_proposal is None
    assert state.last_approval_rejection == "operator denied the write"
    assert state.approved_action_ids == ()


def test_approval_rejection_requires_waiting_with_matching_action() -> None:
    rejected = ApprovalRejected(
        event_id=_event_id(5),
        run_id=RUN,
        occurred_at=NOW,
        sequence=5,
        action_id=ActionId("a1"),
        reason="no",
    )
    with pytest.raises(InvalidTransitionError, match="invalid while run is ready"):
        reduce_event(_state_at("ready"), rejected)
    with pytest.raises(InvalidTransitionError, match="approval event does not match"):
        reduce_event(_state_at("waiting"), replace(rejected, action_id=ActionId("other")))


@pytest.mark.parametrize("stage", ["ready", "reflecting", "waiting"])
def test_operator_instruction_records_steering_and_preserves_status(stage: str) -> None:
    state = reduce_event(_state_at(stage), _operator_instruction(9))

    assert state.operator_instructions == ("focus on the failing test only",)
    assert state.objective == "repair"
    assert state.status is _state_at(stage).status


def test_operator_instruction_can_amend_the_objective() -> None:
    state = reduce_event(
        _state_at("ready"),
        _operator_instruction(3, instruction="only fix adder.py", amends_objective=True),
    )

    assert state.objective == "only fix adder.py"
    assert state.operator_instructions == ("only fix adder.py",)


@pytest.mark.parametrize("stage", ["planning", "acting", "verifying"])
def test_operator_instruction_is_rejected_outside_quiescent_states(stage: str) -> None:
    with pytest.raises(InvalidTransitionError, match="OperatorInstruction is invalid"):
        reduce_event(_state_at(stage), _operator_instruction(9))


@pytest.mark.parametrize(
    ("reason", "expected_status"),
    [
        (StopReason.SUCCESS_VERIFIED, RunStatus.SUCCEEDED),
        (StopReason.FAILURE, RunStatus.FAILED),
        (StopReason.STALLED, RunStatus.STALLED),
        (StopReason.BUDGET_EXHAUSTED, RunStatus.BUDGET_EXHAUSTED),
        (StopReason.MAX_ITERATIONS, RunStatus.FAILED),
        (StopReason.CANCELLED, RunStatus.CANCELLED),
    ],
)
def test_run_stopped_maps_reason_to_terminal_status(
    reason: StopReason, expected_status: RunStatus
) -> None:
    state = reduce_event(_state_at("ready"), _stopped(3, reason=reason))
    assert state.status is expected_status
    assert state.stop_reason is reason


@pytest.mark.parametrize(
    "stage",
    [
        "planning",
        "ready",
        "acting",
        "acting_in_flight",
        "verifying",
        "reflecting",
        "waiting",
    ],
)
def test_run_can_stop_from_every_active_status(stage: str) -> None:
    state = reduce_event(_state_at(stage), _stopped(99, reason=StopReason.CANCELLED))
    assert state.status is RunStatus.CANCELLED
    assert state.stop_reason is StopReason.CANCELLED


# --- transition validation ---


@pytest.mark.parametrize("status", [status for status in RunStatus if status.is_terminal])
def test_terminal_run_rejects_any_event(status: RunStatus) -> None:
    state = replace(RunState(run_id=RUN), status=status)
    with pytest.raises(InvalidTransitionError, match="terminal run cannot accept"):
        reduce_event(state, _budget(9))


def test_terminal_run_cannot_transition_back_to_active() -> None:
    terminal = reduce_event(_state_at("ready"), _stopped(3, reason=StopReason.FAILURE))
    assert terminal.status is RunStatus.FAILED

    with pytest.raises(InvalidTransitionError, match="terminal run cannot accept RunStarted"):
        reduce_event(terminal, _started(4))
    with pytest.raises(InvalidTransitionError, match="terminal run cannot accept RunStopped"):
        reduce_event(terminal, _stopped(4, reason=StopReason.CANCELLED))


@pytest.mark.parametrize(
    ("stage", "event"),
    [
        ("planning", _started(9)),
        ("created", _planned(9)),
        ("planning", _proposed(9)),
        ("planning", _authorized(9)),
        ("planning", _rejected(9)),
        ("ready", _tool_started(9)),
        ("ready", _tool_succeeded(9)),
        ("ready", _tool_failed(9)),
        ("acting", _retry_scheduled(9)),
        ("acting", _circuit_opened(9)),
        ("acting", _verification_passed(9)),
        ("acting", _verification_failed(9)),
        ("verifying", _reflection(9)),
        ("created", _context_assembled(9)),
        ("planning", _context_assembled(9)),
        ("acting", _context_assembled(9)),
        ("verifying", _context_assembled(9)),
        ("reflecting", _context_assembled(9)),
        ("created", _budget(9)),
        ("planning", _approval_requested(9)),
        ("ready", _approval_granted(9)),
        ("created", _stopped(9)),
    ],
)
def test_event_rejected_in_wrong_status(stage: str, event: Event) -> None:
    with pytest.raises(InvalidTransitionError, match="is invalid while run is"):
        reduce_event(_state_at(stage), event)


@pytest.mark.parametrize(
    "event",
    [
        _tool_started(9, action_id="other"),
        _tool_succeeded(9, action_id="other"),
        _tool_failed(9, action_id="other"),
    ],
)
def test_tool_journal_events_require_matching_action_id(event: Event) -> None:
    with pytest.raises(InvalidTransitionError, match="does not match current action"):
        reduce_event(_state_at("acting"), event)


def test_retry_scheduled_requires_matching_action_id() -> None:
    with pytest.raises(InvalidTransitionError, match="does not match current action"):
        reduce_event(_state_at("verifying"), _retry_scheduled(9, action_id="other"))


def test_tool_journal_events_require_an_active_action() -> None:
    acting = replace(_state_at("acting"), current_action_id=None)
    with pytest.raises(InvalidTransitionError, match="does not match current action"):
        reduce_event(acting, _tool_started(9))


def test_first_tool_attempt_must_be_one() -> None:
    with pytest.raises(InvalidTransitionError, match="tool attempt 2 does not match expected 1"):
        reduce_event(_state_at("acting"), _tool_started(5, attempt=2))


def test_next_tool_attempt_must_follow_the_journal() -> None:
    with pytest.raises(InvalidTransitionError, match="tool attempt 1 does not match expected 2"):
        reduce_event(_state_at("acting_in_flight"), _tool_started(6, attempt=1))


@pytest.mark.parametrize(
    "event",
    [
        _tool_succeeded(9, attempt=2),
        _tool_failed(9, attempt=7),
    ],
)
def test_tool_outcome_attempt_must_match_active_attempt(event: Event) -> None:
    with pytest.raises(
        InvalidTransitionError, match="tool outcome attempt does not match active attempt"
    ):
        reduce_event(_state_at("acting_in_flight"), event)


def test_tool_outcome_allowed_without_journal_start_for_legacy_streams() -> None:
    acting = _state_at("acting")
    assert acting.current_attempt == 0

    state = reduce_event(acting, _tool_succeeded(5, observation="ok", attempt=4))

    assert state.status is RunStatus.VERIFYING
    assert state.current_attempt == 4


def test_retry_scheduled_must_be_the_next_journal_attempt() -> None:
    with pytest.raises(
        InvalidTransitionError, match="retry attempt is not the next journal attempt"
    ):
        reduce_event(_state_at("verifying"), _retry_scheduled(7, next_attempt=3))


def test_retry_scheduled_after_legacy_outcome_uses_attempt_floor() -> None:
    verifying = replace(_state_at("verifying"), current_attempt=0)

    state = reduce_event(verifying, _retry_scheduled(7, next_attempt=2))
    assert state.status is RunStatus.ACTING
    assert state.current_attempt == 1

    with pytest.raises(
        InvalidTransitionError, match="retry attempt is not the next journal attempt"
    ):
        reduce_event(verifying, _retry_scheduled(7, next_attempt=3))


# --- replay ---


def test_replay_empty_stream_returns_initial_state() -> None:
    state = replay(RUN, ())
    assert state == RunState(run_id=RUN)


def test_replay_rejects_event_from_another_run() -> None:
    with pytest.raises(ValueError, match="belongs to run"):
        replay(RUN, (_started(1, run_id=RunId("other-run")),))


def test_replay_rejects_stream_not_starting_at_sequence_one() -> None:
    with pytest.raises(ValueError, match="expected sequence 1, got 2"):
        replay(RUN, (_started(2),))


def test_replay_rejects_sequence_gap() -> None:
    with pytest.raises(ValueError, match="expected sequence 2, got 3"):
        replay(RUN, (_started(1), _planned(3)))


def test_replay_rebuilds_completed_run() -> None:
    events: tuple[Event, ...] = (
        _started(1),
        _planned(2),
        _proposed(3),
        _authorized(4),
        _tool_started(5),
        _tool_succeeded(6, observation="all tests pass"),
        _verification_passed(7),
        _stopped(8, reason=StopReason.SUCCESS_VERIFIED),
    )

    state = replay(RUN, events)

    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED
    assert state.version == len(events)
    assert state.iteration == 1
    assert state.last_observation == "all tests pass"
    assert state.last_verification_passed is True


# --- streak and no-progress helpers ---


def test_failure_streak_for_unknown_tool_is_zero() -> None:
    assert RunState(run_id=RUN).failure_streak_for("inspect") == 0
    assert _state_at("verifying").failure_streak_for("other-tool") == 0


def test_failure_streaks_are_tracked_per_tool_and_sorted() -> None:
    state = RunState(run_id=RUN)
    state = _apply_outcome(state, "beta", success=False)
    state = _apply_outcome(state, "alpha", success=False)
    state = _apply_outcome(state, "beta", success=False)
    state = _apply_outcome(state, "alpha", success=True)

    assert state.tool_failure_streaks == (
        ToolFailureStreak("alpha", 0),
        ToolFailureStreak("beta", 2),
    )
    assert state.failure_streak_for("alpha") == 0
    assert state.failure_streak_for("beta") == 2


def test_total_tokens_sums_input_and_output() -> None:
    state = replace(RunState(run_id=RUN), input_tokens=7, output_tokens=3)
    assert state.total_tokens == 10


# --- budget_exceeded ---


def test_budget_exceeded_when_cost_reaches_limit() -> None:
    state = replace(RunState(run_id=RUN), cost_usd=1.0)
    assert budget_exceeded(state, BudgetLimit(1.0, 5))


def test_budget_exceeded_when_iteration_reaches_limit() -> None:
    state = replace(RunState(run_id=RUN), cost_usd=0.5, iteration=5)
    assert budget_exceeded(state, BudgetLimit(1.0, 5))


def test_budget_exceeded_when_total_tokens_reach_limit() -> None:
    state = replace(RunState(run_id=RUN), input_tokens=60, output_tokens=40)
    assert budget_exceeded(state, BudgetLimit(1.0, 5, max_total_tokens=100))


def test_budget_not_exceeded_below_all_limits() -> None:
    state = replace(RunState(run_id=RUN), cost_usd=0.5, iteration=4, input_tokens=99)
    assert not budget_exceeded(state, BudgetLimit(1.0, 5, max_total_tokens=100))


def test_budget_ignores_tokens_when_no_token_limit() -> None:
    state = replace(RunState(run_id=RUN), input_tokens=10_000, output_tokens=10_000)
    assert not budget_exceeded(state, BudgetLimit(1.0, 5))


# --- property tests ---


@settings(derandomize=True, max_examples=60, database=None)
@given(
    usages=st.lists(
        st.builds(
            UsageDelta,
            cost_usd=st.floats(min_value=0.0, max_value=100.0),
            input_tokens=st.integers(min_value=0, max_value=10_000),
            output_tokens=st.integers(min_value=0, max_value=10_000),
            cached_input_tokens=st.integers(min_value=0, max_value=10_000),
        ),
        max_size=30,
    )
)
@example(usages=[])
@example(usages=[UsageDelta(cost_usd=0.1, input_tokens=3, output_tokens=2, cached_input_tokens=1)])
def test_budget_debits_accumulate_additively(usages: list[UsageDelta]) -> None:
    events: list[Event] = [_started(1)]
    events.extend(_budget(index + 2, usage=usage) for index, usage in enumerate(usages))

    state = replay(RUN, tuple(events))

    expected_cost = 0.0
    expected_input = 0
    expected_output = 0
    expected_cached = 0
    for usage in usages:
        expected_cost += usage.cost_usd
        expected_input += usage.input_tokens
        expected_output += usage.output_tokens
        expected_cached += usage.cached_input_tokens

    assert state.version == len(usages) + 1
    assert state.cost_usd == expected_cost
    assert state.input_tokens == expected_input
    assert state.output_tokens == expected_output
    assert state.cached_input_tokens == expected_cached
    assert state.total_tokens == expected_input + expected_output


@settings(derandomize=True, max_examples=60, database=None)
@given(
    outcomes=st.lists(
        st.tuples(st.sampled_from(["alpha", "beta", "gamma"]), st.booleans()),
        max_size=25,
    )
)
@example(outcomes=[])
@example(outcomes=[("beta", False), ("alpha", False), ("beta", False), ("alpha", True)])
def test_failure_streaks_match_reference_model(outcomes: list[tuple[str, bool]]) -> None:
    state = RunState(run_id=RUN)
    expected: dict[str, int] = {}
    for tool_name, success in outcomes:
        state = _apply_outcome(state, tool_name, success=success)
        expected[tool_name] = 0 if success else expected.get(tool_name, 0) + 1

    assert state.tool_failure_streaks == tuple(
        ToolFailureStreak(name, expected[name]) for name in sorted(expected)
    )
    for tool_name, count in expected.items():
        assert state.failure_streak_for(tool_name) == count


@settings(derandomize=True, max_examples=60, database=None)
@given(
    rounds=st.lists(
        st.tuples(
            st.sampled_from(["a", "b", "c"]),
            st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0)),
        ),
        max_size=20,
    )
)
@example(rounds=[])
@example(rounds=[("a", 0.5), ("a", 0.5), ("b", None), ("b", None), ("a", 0.7)])
def test_no_progress_tracking_matches_reference_model(
    rounds: list[tuple[str, float | None]],
) -> None:
    state = RunState(run_id=RUN)
    expected_best: float | None = None
    expected_last: str | None = None
    expected_no_progress = 0
    for summary, score in rounds:
        state = _apply_verification_failure(state, summary=summary, score=score)
        if score is not None:
            if expected_best is None or score > expected_best:
                expected_best = score
                expected_no_progress = 0
            else:
                expected_no_progress += 1
        elif expected_last is None or summary != expected_last:
            expected_no_progress = 0
        else:
            expected_no_progress += 1
        expected_last = summary

    assert state.best_verification_score == expected_best
    assert state.last_verification == expected_last
    assert state.consecutive_no_progress == expected_no_progress


# --- PACS-010 hardening: catalog completeness and artifact projection pins ---


def test_artifact_recorded_projects_a_content_fingerprint_for_dedup() -> None:
    state = reduce_event(_state_at("verifying"), _artifact_recorded(7))

    assert len(state.recorded_artifacts) == 1
    fingerprint = state.recorded_artifacts[0]
    assert fingerprint.kind == "workspace_snapshot"
    assert fingerprint.label == "workspace:fixture"
    assert fingerprint.content.startswith("workspace_id=fixture")


# --- PACS-013: worker lifecycle roster projection (orchestrator streams) ---


def _worker_spawned(
    sequence: int, worker_id: str = "adder", *, worker_run_id: str = "run_worker"
) -> WorkerSpawned:
    return WorkerSpawned(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        worker_id=WorkerId(worker_id),
        worker_run_id=RunId(worker_run_id),
        workspace_id=WorkspaceId(f"calculator-{worker_id}"),
        objective=f"repair {worker_id}.py",
        budget_share_cost_usd=1.0,
    )


def _worker_stopped(
    sequence: int,
    worker_id: str = "adder",
    outcome: WorkerOutcome = WorkerOutcome.SUCCEEDED,
) -> WorkerStopped:
    return WorkerStopped(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        worker_id=WorkerId(worker_id),
        outcome=outcome,
        summary=f"worker {worker_id} finished",
    )


def _worker_merged(
    sequence: int,
    worker_id: str = "adder",
    outcome: MergeOutcome = MergeOutcome.MERGED,
) -> WorkerMerged:
    return WorkerMerged(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        worker_id=WorkerId(worker_id),
        outcome=outcome,
        revision="abc123" if outcome is MergeOutcome.MERGED else None,
        detail="merged" if outcome is MergeOutcome.MERGED else "conflicted and aborted",
    )


def _orchestrator_state_at_acting() -> RunState:
    return replay(RUN, (_started(1), _planned(2), _worker_spawned(3)))


def test_worker_spawned_projects_roster_entry_and_enters_acting() -> None:
    state = _orchestrator_state_at_acting()

    assert state.status is RunStatus.ACTING
    assert len(state.workers) == 1
    projection = state.workers[0]
    assert projection.worker_id == WorkerId("adder")
    assert projection.worker_run_id == RunId("run_worker")
    assert projection.workspace_id == WorkspaceId("calculator-adder")
    assert projection.budget_share_cost_usd == 1.0
    assert projection.outcome is None
    assert projection.merge_outcome is None


def test_worker_spawned_is_rejected_before_the_plan_exists() -> None:
    with pytest.raises(InvalidTransitionError, match="invalid while run is planning"):
        replay(RUN, (_started(1), _worker_spawned(2)))


def test_worker_spawned_rejects_a_duplicate_worker_id() -> None:
    with pytest.raises(InvalidTransitionError, match="already on the roster"):
        replay(RUN, (_started(1), _planned(2), _worker_spawned(3), _worker_spawned(4)))


def test_worker_stopped_records_the_terminal_outcome() -> None:
    state = reduce_event(_orchestrator_state_at_acting(), _worker_stopped(4))

    assert state.workers[0].outcome is WorkerOutcome.SUCCEEDED
    assert state.status is RunStatus.ACTING


def test_worker_stopped_rejects_a_second_terminal_outcome() -> None:
    state = reduce_event(_orchestrator_state_at_acting(), _worker_stopped(4))
    with pytest.raises(InvalidTransitionError, match="already has a terminal outcome"):
        reduce_event(state, _worker_stopped(5, outcome=WorkerOutcome.FAILED))


def test_worker_stopped_rejects_an_unknown_worker() -> None:
    with pytest.raises(InvalidTransitionError, match="unknown worker"):
        reduce_event(_orchestrator_state_at_acting(), _worker_stopped(4, worker_id="ghost"))


def test_worker_merged_requires_a_succeeded_worker() -> None:
    with pytest.raises(InvalidTransitionError, match="only a succeeded worker may merge"):
        reduce_event(_orchestrator_state_at_acting(), _worker_merged(4))


def test_worker_merged_rejects_a_failed_worker_merge() -> None:
    state = reduce_event(
        _orchestrator_state_at_acting(),
        _worker_stopped(4, outcome=WorkerOutcome.FAILED),
    )
    with pytest.raises(InvalidTransitionError, match="only a succeeded worker may merge"):
        reduce_event(state, _worker_merged(5))


def test_worker_merged_enters_verifying_once_every_worker_is_resolved() -> None:
    state = reduce_event(_orchestrator_state_at_acting(), _worker_stopped(4))
    state = reduce_event(state, _worker_merged(5))

    assert state.workers[0].merge_outcome is MergeOutcome.MERGED
    assert state.status is RunStatus.VERIFYING


def test_worker_merged_rejects_a_second_merge_outcome() -> None:
    # Two workers keep the run ACTING after the first merge, isolating the
    # duplicate-merge guard from the resolved-roster VERIFYING transition.
    events: tuple[Event, ...] = (
        _started(1),
        _planned(2),
        _worker_spawned(3, "adder", worker_run_id="run_adder"),
        _worker_spawned(4, "greeter", worker_run_id="run_greeter"),
        _worker_stopped(5, "adder"),
        _worker_merged(6, "adder"),
    )
    state = replay(RUN, events)
    with pytest.raises(InvalidTransitionError, match="already has a merge outcome"):
        reduce_event(state, _worker_merged(7, "adder", MergeOutcome.CONFLICT))


def test_roster_stays_acting_until_the_last_worker_merges() -> None:
    events: tuple[Event, ...] = (
        _started(1),
        _planned(2),
        _worker_spawned(3, "adder", worker_run_id="run_adder"),
        _worker_spawned(4, "greeter", worker_run_id="run_greeter"),
        _worker_stopped(5, "adder"),
        _worker_merged(6, "adder"),
        _worker_stopped(7, "greeter"),
    )
    state = replay(RUN, events)
    assert state.status is RunStatus.ACTING  # WorkerStopped never transitions alone.

    state = reduce_event(state, _worker_merged(8, "greeter"))
    assert state.status is RunStatus.VERIFYING


def test_worker_merged_conflict_still_resolves_the_worker() -> None:
    # A conflicted merge is aborted and recorded explicitly; the worker counts
    # as resolved (the orchestrator then stops the run FAILURE).
    state = reduce_event(_orchestrator_state_at_acting(), _worker_stopped(4))
    state = reduce_event(state, _worker_merged(5, outcome=MergeOutcome.CONFLICT))

    assert state.workers[0].merge_outcome is MergeOutcome.CONFLICT
    assert state.status is RunStatus.VERIFYING
