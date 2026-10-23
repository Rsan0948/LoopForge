"""Classify recorded measurements, never worker prose, for bounded recovery."""

from collections.abc import Sequence

from loopforge.domain.events import DomainEvent, ModelAttemptFailed, RunStopped, VerificationFailed
from loopforge.domain.investigation import FailureKind
from loopforge.domain.types import StopReason


def worker_failure(events: Sequence[DomainEvent]) -> FailureKind:  # noqa: PLR0912 - explicit failure taxonomy
    for event in reversed(events):
        if isinstance(event, RunStopped):
            if event.reason == StopReason.STALLED:
                return FailureKind.NO_PROGRESS
            if event.reason in {StopReason.BUDGET_EXHAUSTED, StopReason.MAX_ITERATIONS}:
                return FailureKind.POLICY
        if isinstance(event, ModelAttemptFailed):
            failure = FailureKind.PROVIDER
            if event.reason_code in {"CODING_NO_NEW_EVIDENCE", "CODING_REPAIR_UNABLE"}:
                failure = FailureKind.NO_PROGRESS
            elif event.reason_code == "REVIEW_CHANGES_REQUESTED":
                # A completed review found a defect in the patch: a verification outcome.
                failure = FailureKind.VERIFICATION
            elif event.reason_code == "MODEL_INVALID_RESPONSE" or event.reason_code.startswith(
                ("REPAIR_", "MODEL_TOOL_", "MODEL_ACTION_", "REVIEW_PROTOCOL_")
            ):
                failure = FailureKind.PROTOCOL
            elif event.reason_code.startswith(("CODING_STALLED", "CODING_HYPOTHESIS")):
                failure = FailureKind.HYPOTHESIS
            elif event.reason_code.startswith(("AUTHORITY_", "PERMISSION_", "POLICY_")):
                failure = FailureKind.AUTHORITY
            elif event.reason_code.startswith("REVIEW_CONTEXT_"):
                failure = FailureKind.CONTEXT
            elif event.reason_code in {"REVIEW_BUDGET_EXHAUSTED", "REVIEW_PRICING_EXPIRED"}:
                failure = FailureKind.POLICY
            elif event.context_admission is not None and not event.context_admission.admitted:
                failure = FailureKind.CONTEXT
            return failure
        if isinstance(event, VerificationFailed):
            return FailureKind.ENVIRONMENT if event.inconclusive else FailureKind.VERIFICATION
    return FailureKind.UNKNOWN
