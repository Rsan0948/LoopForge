"""Unit tests for `loopforge.domain.events` validation rules and payloads."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import get_args

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import MAX_ARTIFACT_CONTENT_BYTES, ArtifactKind
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
    DomainEvent,
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
    ContextItemId,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    StopReason,
    UsageDelta,
    WorkerId,
    WorkspaceId,
)

NOW = datetime(2026, 8, 22, tzinfo=UTC)
RUN = RunId("events-run")

ALL_EVENT_CLASSES: tuple[type[DomainEvent], ...] = (
    RunStarted,
    PlanCreated,
    ActionProposed,
    ActionAuthorized,
    ActionRejected,
    ToolExecutionStarted,
    ToolSucceeded,
    ToolFailed,
    RetryScheduled,
    CircuitOpened,
    VerificationPassed,
    VerificationFailed,
    ReflectionRecorded,
    ArtifactRecorded,
    BudgetDebited,
    ApprovalRequested,
    ApprovalGranted,
    ApprovalRejected,
    OperatorInstruction,
    RunStopped,
    ContextAssembled,
    WorkerSpawned,
    WorkerStopped,
    WorkerMerged,
)


class _OffsetlessTimezone(tzinfo):
    """A tzinfo whose UTC offset is unknown, making datetimes effectively naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        del dt  # the offset is intentionally unknown regardless of the instant
        return None


def _proposal() -> ActionProposal:
    return ActionProposal(ActionId("a1"), "inspect", {})


def _metadata() -> ToolMetadata:
    return ToolMetadata(
        name="inspect",
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _context_snapshot() -> ContextItemSnapshot:
    return ContextItemSnapshot(
        item_id=ContextItemId("run-1:objective"),
        content="repair auth",
        trust=TrustClass.AUTHORIZED_HUMAN,
        source=ContextSource(
            origin=TrustClass.AUTHORIZED_HUMAN,
            reference="run:run-1:objective",
            detail="operator-supplied run objective",
        ),
        sensitivity=DataSensitivity.INTERNAL,
        created_at=NOW,
    )


def _tool_started(*, attempt: int, sequence: int = 1) -> ToolExecutionStarted:
    return ToolExecutionStarted(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        action_id=ActionId("a1"),
        attempt=attempt,
        idempotency_key=None,
    )


def _tool_succeeded(*, attempt: int, sequence: int = 1) -> ToolSucceeded:
    return ToolSucceeded(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        action_id=ActionId("a1"),
        observation="ok",
        attempt=attempt,
    )


def _tool_failed(*, attempt: int, sequence: int = 1) -> ToolFailed:
    return ToolFailed(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        action_id=ActionId("a1"),
        error_code="E_IO",
        error_message="disk unavailable",
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=attempt,
    )


def _retry_scheduled(*, next_attempt: int, delay_seconds: float) -> RetryScheduled:
    return RetryScheduled(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        action_id=ActionId("a1"),
        next_attempt=next_attempt,
        delay_seconds=delay_seconds,
        reason_code="RETRY_TRANSIENT_FAILURE",
    )


def _all_events() -> tuple[Event, ...]:
    return (
        RunStarted(
            event_id=EventId("e1"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=1,
            objective="repair auth",
        ),
        PlanCreated(
            event_id=EventId("e2"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=2,
            plan="inspect",
        ),
        ActionProposed(
            event_id=EventId("e3"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=3,
            proposal=_proposal(),
        ),
        ActionAuthorized(
            event_id=EventId("e4"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=4,
            proposal=_proposal(),
            tool_metadata=_metadata(),
        ),
        ActionRejected(
            event_id=EventId("e5"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=5,
            proposal=_proposal(),
            reason_code="PERMISSION_DENIED",
        ),
        _tool_started(attempt=1),
        _tool_succeeded(attempt=1),
        _tool_failed(attempt=1),
        _retry_scheduled(next_attempt=2, delay_seconds=0.25),
        CircuitOpened(
            event_id=EventId("e9"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=9,
            tool_name="inspect",
            reason_code="CIRCUIT_THRESHOLD",
        ),
        VerificationPassed(
            event_id=EventId("e10"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=10,
            summary="all tests pass",
        ),
        VerificationFailed(
            event_id=EventId("e11"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=11,
            summary="tests fail",
        ),
        ReflectionRecorded(
            event_id=EventId("e12"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=12,
            reflection="try a smaller diff",
        ),
        ArtifactRecorded(
            event_id=EventId("e18"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=18,
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="workspace:adder-regression",
            content="workspace_id=adder-regression\n\ndiff --git a/adder.py b/adder.py",
        ),
        BudgetDebited(
            event_id=EventId("e13"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=13,
            usage=UsageDelta(cost_usd=0.01, input_tokens=1, output_tokens=1),
        ),
        ApprovalRequested(
            event_id=EventId("e14"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=14,
            action_id=ActionId("a1"),
            reason="irreversible action",
        ),
        ApprovalGranted(
            event_id=EventId("e15"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=15,
            action_id=ActionId("a1"),
        ),
        ApprovalRejected(
            event_id=EventId("e23"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=23,
            action_id=ActionId("a1"),
            reason="operator denied the write",
        ),
        OperatorInstruction(
            event_id=EventId("e24"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=24,
            instruction="focus on the failing test only",
            amends_objective=False,
        ),
        RunStopped(
            event_id=EventId("e16"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=16,
            reason=StopReason.SUCCESS_VERIFIED,
            summary="done",
        ),
        ContextAssembled(
            event_id=EventId("e17"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=17,
            context_items=(_context_snapshot(),),
        ),
        WorkerSpawned(
            event_id=EventId("e19"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=19,
            worker_id=WorkerId("worker-adder"),
            worker_run_id=RunId("worker-run-1"),
            workspace_id=WorkspaceId("ws-adder"),
            objective="repair adder.py",
            budget_share_cost_usd=0.5,
        ),
        WorkerStopped(
            event_id=EventId("e20"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=20,
            worker_id=WorkerId("worker-adder"),
            outcome=WorkerOutcome.SUCCEEDED,
            summary="worker finished",
        ),
        WorkerMerged(
            event_id=EventId("e21"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=21,
            worker_id=WorkerId("worker-adder"),
            outcome=MergeOutcome.MERGED,
            revision="a" * 40,
            detail="merged worker branch",
        ),
    )


def test_artifact_recorded_rejects_blank_label() -> None:
    with pytest.raises(ValueError, match="artifact label cannot be empty"):
        ArtifactRecorded(
            event_id=EventId("e1"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=1,
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="  ",
            content="evidence",
        )


def test_domain_event_accepts_positive_sequence_and_aware_timestamp() -> None:
    event = DomainEvent(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
    )

    assert event.sequence == 1
    assert event.run_id == RUN
    assert event.caused_by is None


def test_domain_event_accepts_non_utc_aware_timestamp() -> None:
    shifted = datetime(2026, 8, 22, 12, 0, tzinfo=timezone(timedelta(hours=-5)))

    event = DomainEvent(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=shifted,
        sequence=1,
    )

    assert event.occurred_at is shifted


@pytest.mark.parametrize("sequence", [0, -1, -100])
def test_domain_event_rejects_non_positive_sequence(sequence: int) -> None:
    with pytest.raises(ValueError, match="event sequence must be positive"):
        DomainEvent(
            event_id=EventId("e1"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=sequence,
        )


def test_domain_event_rejects_naive_occurred_at() -> None:
    naive = datetime(2026, 8, 22, 12, 0, 0)  # noqa: DTZ001

    with pytest.raises(ValueError, match="occurred_at must be timezone-aware"):
        DomainEvent(
            event_id=EventId("e1"),
            run_id=RUN,
            occurred_at=naive,
            sequence=1,
        )


def test_domain_event_rejects_tzinfo_without_utcoffset() -> None:
    ambiguous = datetime(2026, 8, 22, tzinfo=_OffsetlessTimezone())

    with pytest.raises(ValueError, match="occurred_at must be timezone-aware"):
        DomainEvent(
            event_id=EventId("e1"),
            run_id=RUN,
            occurred_at=ambiguous,
            sequence=1,
        )


def test_tool_event_validation_inherits_base_event_rules() -> None:
    with pytest.raises(ValueError, match="event sequence must be positive"):
        _tool_succeeded(attempt=1, sequence=0)

    with pytest.raises(ValueError, match="occurred_at must be timezone-aware"):
        RetryScheduled(
            event_id=EventId("e1"),
            run_id=RUN,
            occurred_at=datetime(2026, 8, 22, 12, 0, 0),  # noqa: DTZ001
            sequence=1,
            action_id=ActionId("a1"),
            next_attempt=2,
            delay_seconds=0.0,
            reason_code="RETRY_TRANSIENT_FAILURE",
        )


def test_tool_execution_started_records_attempt_and_idempotency_key() -> None:
    event = ToolExecutionStarted(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        action_id=ActionId("a1"),
        attempt=2,
        idempotency_key="loopforge:events-run:a1",
    )

    assert event.attempt == 2
    assert event.idempotency_key == "loopforge:events-run:a1"


@pytest.mark.parametrize("attempt", [0, -1])
def test_tool_execution_started_rejects_non_positive_attempt(attempt: int) -> None:
    with pytest.raises(ValueError, match="tool attempt must be positive"):
        _tool_started(attempt=attempt)


def test_tool_succeeded_defaults_to_first_attempt() -> None:
    event = ToolSucceeded(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        action_id=ActionId("a1"),
        observation="all tests pass",
    )

    assert event.attempt == 1
    assert event.observation == "all tests pass"


@pytest.mark.parametrize("attempt", [0, -1])
def test_tool_succeeded_rejects_non_positive_attempt(attempt: int) -> None:
    with pytest.raises(ValueError, match="tool attempt must be positive"):
        _tool_succeeded(attempt=attempt)


def test_tool_failed_defaults_to_first_attempt_and_records_failure() -> None:
    event = _tool_failed(attempt=1)

    assert event.attempt == 1
    assert event.error_code == "E_IO"
    assert event.error_message == "disk unavailable"
    assert event.failure_class is ToolFailureClass.TRANSIENT


@pytest.mark.parametrize("attempt", [0, -1])
def test_tool_failed_rejects_non_positive_attempt(attempt: int) -> None:
    with pytest.raises(ValueError, match="tool attempt must be positive"):
        _tool_failed(attempt=attempt)


def test_retry_scheduled_accepts_zero_delay_for_second_attempt() -> None:
    event = _retry_scheduled(next_attempt=2, delay_seconds=0.0)

    assert event.next_attempt == 2
    assert event.delay_seconds == 0.0
    assert event.reason_code == "RETRY_TRANSIENT_FAILURE"


@pytest.mark.parametrize("next_attempt", [1, 0, -3])
def test_retry_scheduled_rejects_next_attempt_at_or_below_one(next_attempt: int) -> None:
    with pytest.raises(ValueError, match="retry next_attempt must be greater than one"):
        _retry_scheduled(next_attempt=next_attempt, delay_seconds=0.0)


def test_retry_scheduled_rejects_negative_delay() -> None:
    with pytest.raises(ValueError, match="retry delay cannot be negative"):
        _retry_scheduled(next_attempt=2, delay_seconds=-0.1)


def test_run_started_records_objective() -> None:
    event = RunStarted(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        objective="repair auth",
    )

    assert event.objective == "repair auth"


def test_plan_created_records_plan_and_causal_link() -> None:
    started = RunStarted(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        objective="repair auth",
    )
    planned = PlanCreated(
        event_id=EventId("e2"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=2,
        caused_by=started.event_id,
        plan="inspect",
    )

    assert planned.plan == "inspect"
    assert planned.caused_by == started.event_id


def test_action_proposed_records_proposal() -> None:
    proposal = _proposal()
    event = ActionProposed(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        proposal=proposal,
    )

    assert event.proposal is proposal


def test_action_authorized_records_proposal_and_tool_metadata() -> None:
    proposal = _proposal()
    metadata = _metadata()
    event = ActionAuthorized(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        proposal=proposal,
        tool_metadata=metadata,
    )

    assert event.proposal is proposal
    assert event.tool_metadata is metadata


def test_action_rejected_records_proposal_and_reason_code() -> None:
    proposal = _proposal()
    event = ActionRejected(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        proposal=proposal,
        reason_code="PERMISSION_DENIED",
    )

    assert event.proposal is proposal
    assert event.reason_code == "PERMISSION_DENIED"


def test_circuit_opened_records_tool_and_reason() -> None:
    event = CircuitOpened(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        tool_name="inspect",
        reason_code="CIRCUIT_THRESHOLD",
    )

    assert event.tool_name == "inspect"
    assert event.reason_code == "CIRCUIT_THRESHOLD"


def test_verification_passed_records_summary() -> None:
    event = VerificationPassed(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        summary="all tests pass",
    )

    assert event.summary == "all tests pass"


def test_verification_failed_records_summary_and_optional_score() -> None:
    without_score = VerificationFailed(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        summary="tests fail",
    )
    with_score = VerificationFailed(
        event_id=EventId("e2"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=2,
        summary="tests fail",
        score=0.25,
    )

    assert without_score.score is None
    assert with_score.score == 0.25


def test_reflection_recorded_records_reflection() -> None:
    event = ReflectionRecorded(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        reflection="try a smaller diff",
    )

    assert event.reflection == "try a smaller diff"


def test_budget_debited_records_usage() -> None:
    usage = UsageDelta(cost_usd=0.01, input_tokens=1, output_tokens=1)
    event = BudgetDebited(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        usage=usage,
    )

    assert event.usage is usage


def test_approval_requested_records_action_and_reason() -> None:
    event = ApprovalRequested(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        action_id=ActionId("a1"),
        reason="irreversible action",
    )

    assert event.action_id == ActionId("a1")
    assert event.reason == "irreversible action"


def test_approval_granted_records_action() -> None:
    event = ApprovalGranted(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        action_id=ActionId("a1"),
    )

    assert event.action_id == ActionId("a1")


def test_run_stopped_records_reason_and_summary() -> None:
    event = RunStopped(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        reason=StopReason.SUCCESS_VERIFIED,
        summary="done",
    )

    assert event.reason is StopReason.SUCCESS_VERIFIED
    assert event.summary == "done"


def test_every_event_class_constructs_with_valid_payloads() -> None:
    events = _all_events()

    assert len(events) == len(ALL_EVENT_CLASSES)
    assert all(isinstance(event, DomainEvent) for event in events)
    assert [type(event) for event in events] == list(ALL_EVENT_CLASSES)


def test_event_union_matches_all_concrete_event_classes() -> None:
    union_members = set(get_args(Event))

    assert union_members == set(ALL_EVENT_CLASSES)
    assert DomainEvent not in union_members


def test_events_are_immutable() -> None:
    for event in _all_events():
        with pytest.raises(FrozenInstanceError, match="cannot assign to field"):
            # Deliberate mutation probe: frozen events must reject attribute writes.
            event.sequence = 99  # pyright: ignore[reportAttributeAccessIssue]


@example(sequence=1, occurred_at=NOW)
@settings(derandomize=True, max_examples=25)
@given(sequence=st.integers(min_value=1), occurred_at=st.datetimes(timezones=st.timezones()))
def test_any_positive_sequence_and_aware_timestamp_is_accepted(
    sequence: int, occurred_at: datetime
) -> None:
    event = DomainEvent(
        event_id=EventId("e1"),
        run_id=RUN,
        occurred_at=occurred_at,
        sequence=sequence,
    )

    assert event.sequence == sequence
    assert event.occurred_at == occurred_at


@example(sequence=0)
@settings(derandomize=True, max_examples=25)
@given(sequence=st.integers(max_value=0))
def test_any_non_positive_sequence_is_rejected(sequence: int) -> None:
    with pytest.raises(ValueError, match="event sequence must be positive"):
        DomainEvent(
            event_id=EventId("e1"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=sequence,
        )


@example(attempt=1)
@settings(derandomize=True, max_examples=25)
@given(attempt=st.integers(min_value=1))
def test_any_positive_attempt_is_accepted(attempt: int) -> None:
    started = _tool_started(attempt=attempt)
    succeeded = _tool_succeeded(attempt=attempt)
    failed = _tool_failed(attempt=attempt)

    assert started.attempt == attempt
    assert succeeded.attempt == attempt
    assert failed.attempt == attempt


@example(attempt=0)
@settings(derandomize=True, max_examples=25)
@given(attempt=st.integers(max_value=0))
def test_any_non_positive_attempt_is_rejected(attempt: int) -> None:
    with pytest.raises(ValueError, match="tool attempt must be positive"):
        _tool_started(attempt=attempt)
    with pytest.raises(ValueError, match="tool attempt must be positive"):
        _tool_succeeded(attempt=attempt)
    with pytest.raises(ValueError, match="tool attempt must be positive"):
        _tool_failed(attempt=attempt)


@example(next_attempt=2, delay_seconds=0.0)
@settings(derandomize=True, max_examples=25)
@given(
    next_attempt=st.integers(min_value=2),
    delay_seconds=st.floats(min_value=0.0, allow_nan=False, allow_infinity=False),
)
def test_retry_scheduled_accepts_any_later_attempt_and_non_negative_delay(
    next_attempt: int, delay_seconds: float
) -> None:
    event = _retry_scheduled(next_attempt=next_attempt, delay_seconds=delay_seconds)

    assert event.next_attempt == next_attempt
    assert event.delay_seconds == delay_seconds


@example(next_attempt=1)
@settings(derandomize=True, max_examples=25)
@given(next_attempt=st.integers(max_value=1))
def test_retry_scheduled_rejects_any_next_attempt_at_or_below_one(next_attempt: int) -> None:
    with pytest.raises(ValueError, match="retry next_attempt must be greater than one"):
        _retry_scheduled(next_attempt=next_attempt, delay_seconds=0.0)


@example(delay_seconds=-0.5)
@settings(derandomize=True, max_examples=25)
@given(
    delay_seconds=st.floats(max_value=0.0, exclude_max=True, allow_nan=False, allow_infinity=False)
)
def test_retry_scheduled_rejects_any_negative_delay(delay_seconds: float) -> None:
    with pytest.raises(ValueError, match="retry delay cannot be negative"):
        _retry_scheduled(next_attempt=2, delay_seconds=delay_seconds)


# --- PACS-010 hardening: event-construction validation pins ---


def _base_fields() -> dict[str, object]:
    return {
        "event_id": EventId("e1"),
        "run_id": RUN,
        "occurred_at": NOW,
        "sequence": 1,
    }


def test_artifact_recorded_rejects_invalid_kind_label_and_oversized_content() -> None:
    with pytest.raises(TypeError, match="kind must be an ArtifactKind"):
        ArtifactRecorded(
            **_base_fields(),  # pyright: ignore[reportArgumentType]  # dict[str, object] fixture unpacking
            kind="workspace_snapshot",  # pyright: ignore[reportArgumentType]  # intentional invalid kind
            label="a",
            content="c",
        )
    with pytest.raises(ValueError, match="control characters"):
        ArtifactRecorded(
            **_base_fields(),  # pyright: ignore[reportArgumentType]
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="forged\nlabel",
            content="c",
        )
    with pytest.raises(ValueError, match="byte budget"):
        ArtifactRecorded(
            **_base_fields(),  # pyright: ignore[reportArgumentType]
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="a",
            content="x" * (MAX_ARTIFACT_CONTENT_BYTES + 1),
        )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -0.5, 1.5])
def test_verification_failed_rejects_non_finite_or_out_of_range_scores(bad: float) -> None:
    with pytest.raises(ValueError, match="finite fraction"):
        VerificationFailed(
            **_base_fields(),  # pyright: ignore[reportArgumentType]
            summary="still broken",
            score=bad,
        )


def test_verification_failed_accepts_boundary_scores() -> None:
    for boundary in (0.0, 1.0):
        event = VerificationFailed(
            **_base_fields(),  # pyright: ignore[reportArgumentType]
            summary="s",
            score=boundary,
        )
        assert event.score == boundary


def test_transition_table_covers_the_entire_event_catalog() -> None:
    # Drift guard: a new event type without a reducer transition arm would
    # otherwise surface as a raw KeyError at reduction time instead of a
    # deliberate contract failure.
    from loopforge.domain.state import (  # noqa: PLC0415 - private catalog pinned at the point of use, not at module scope
        _ALLOWED_STATUS,  # pyright: ignore[reportPrivateUsage]  # catalog pinned directly
    )

    assert set(_ALLOWED_STATUS) == set(ALL_EVENT_CLASSES)
