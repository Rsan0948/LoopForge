"""Recorded failure categories guide recovery without interpreting model claims."""

from datetime import UTC, datetime

import pytest

from loopforge.application.failure_recovery import worker_failure
from loopforge.domain.admission import ContextAdmission, InputMeasurementBasis
from loopforge.domain.events import DomainEvent, ModelAttemptFailed, RunStopped, VerificationFailed
from loopforge.domain.investigation import FailureKind
from loopforge.domain.types import EventId, RunId, StopReason

NOW = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            VerificationFailed(
                event_id=EventId("e"),
                run_id=RunId("r"),
                occurred_at=NOW,
                sequence=1,
                summary="worker claims success",
                inconclusive=False,
            ),
            FailureKind.VERIFICATION,
        ),
        (
            VerificationFailed(
                event_id=EventId("e"),
                run_id=RunId("r"),
                occurred_at=NOW,
                sequence=1,
                summary="unexecuted checks",
                inconclusive=True,
            ),
            FailureKind.ENVIRONMENT,
        ),
        (
            ModelAttemptFailed(
                event_id=EventId("e"),
                run_id=RunId("r"),
                occurred_at=NOW,
                sequence=1,
                reason_code="MODEL_INVALID_RESPONSE",
                summary="bad JSON",
            ),
            FailureKind.PROTOCOL,
        ),
        (
            ModelAttemptFailed(
                event_id=EventId("e"),
                run_id=RunId("r"),
                occurred_at=NOW,
                sequence=1,
                reason_code="REVIEW_CHANGES_REQUESTED",
                summary="Review requested changes: 409 conflict assertion removed",
            ),
            FailureKind.VERIFICATION,
        ),
        (
            ModelAttemptFailed(
                event_id=EventId("e"),
                run_id=RunId("r"),
                occurred_at=NOW,
                sequence=1,
                reason_code="CONTEXT_ADMISSION",
                summary="not admitted",
                context_admission=ContextAdmission(
                    measurement_basis=InputMeasurementBasis.UTF8_BYTE_UPPER_BOUND,
                    input_token_bound=100,
                    serialized_request_bytes=100,
                    context_window_tokens=100,
                    output_reserve_tokens=20,
                    framing_reserve_tokens=10,
                ),
            ),
            FailureKind.CONTEXT,
        ),
        (
            RunStopped(
                event_id=EventId("e"),
                run_id=RunId("r"),
                occurred_at=NOW,
                sequence=1,
                reason=StopReason.STALLED,
                summary="no evidence or patch changes",
            ),
            FailureKind.NO_PROGRESS,
        ),
        (
            RunStopped(
                event_id=EventId("e"),
                run_id=RunId("r"),
                occurred_at=NOW,
                sequence=1,
                reason=StopReason.BUDGET_EXHAUSTED,
                summary="aggregate token allowance",
            ),
            FailureKind.POLICY,
        ),
    ],
)
def test_typed_measurements_determine_failure(event: DomainEvent, expected: FailureKind) -> None:
    assert worker_failure([event]) == expected


def test_unknown_or_empty_history_does_not_invent_a_diagnosis() -> None:
    assert worker_failure([]) == FailureKind.UNKNOWN


def test_latest_measurement_wins_over_earlier_provider_failure() -> None:
    provider = ModelAttemptFailed(
        event_id=EventId("provider"),
        run_id=RunId("r"),
        occurred_at=NOW,
        sequence=1,
        reason_code="MODEL_INVALID_RESPONSE",
        summary="bad JSON",
    )
    behavior = VerificationFailed(
        event_id=EventId("check"),
        run_id=RunId("r"),
        occurred_at=NOW,
        sequence=2,
        summary="real failing criterion",
    )
    assert worker_failure([provider, behavior]) == FailureKind.VERIFICATION
