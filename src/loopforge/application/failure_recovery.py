"""Classify recorded measurements, never worker prose, for bounded recovery."""

from collections.abc import Sequence
from typing import Final

from loopforge.domain.events import DomainEvent, ModelAttemptFailed, RunStopped, VerificationFailed
from loopforge.domain.investigation import FailureKind
from loopforge.domain.types import StopReason

# Every model-attempt reason code, mapped explicitly. A code that is not here is
# UNKNOWN, never a guess (it used to default to PROVIDER, which misreported harness
# gates as provider outages).
REASON_KINDS: Final[dict[str, FailureKind]] = {
    # The provider or its transport failed.
    "MODEL_UNAVAILABLE": FailureKind.PROVIDER,
    "MODEL_TIMEOUT": FailureKind.PROVIDER,
    "MODEL_AUTHENTICATION": FailureKind.PROVIDER,
    "MODEL_NOT_FOUND": FailureKind.PROVIDER,
    "REVIEW_PROVIDER_FAILURE": FailureKind.PROVIDER,
    "REPORT_PROVIDER_FAILURE": FailureKind.PROVIDER,
    "REPORT_USAGE_UNKNOWN": FailureKind.PROVIDER,
    # The model's output did not follow the protocol.
    "MODEL_INVALID_RESPONSE": FailureKind.PROTOCOL,
    "MODEL_TOOL_PARSE": FailureKind.PROTOCOL,
    "REPORT_INCOMPLETE": FailureKind.PROTOCOL,
    "REPORT_INVALID_JSON": FailureKind.PROTOCOL,
    "REPORT_INVALID_RESPONSE": FailureKind.PROTOCOL,
    "REPORT_EVIDENCE_REQUIRED": FailureKind.PROTOCOL,
    "REPORT_PREFLIGHT_SOURCE": FailureKind.PROTOCOL,
    "REVIEW_REPAIR_REQUIRED": FailureKind.PROTOCOL,
    "REVIEW_REQUIRED_EVIDENCE_INVALID": FailureKind.PROTOCOL,
    # Reviewer tool circuits open on the reviewer's own repeated invalid calls.
    "REVIEW_SOURCE_CIRCUIT_OPEN": FailureKind.PROTOCOL,
    "REVIEW_REPORT_CIRCUIT_OPEN": FailureKind.PROTOCOL,
    # The work did not fit the context or packet limits.
    "MODEL_CONTEXT_LIMIT": FailureKind.CONTEXT,
    "REPORT_PACKET_LIMIT": FailureKind.CONTEXT,
    "REVIEW_REQUIRED_READ_LIMIT": FailureKind.CONTEXT,
    # A code-owned allowance ran out.
    "MODEL_DEADLINE": FailureKind.POLICY,
    "REVIEW_BUDGET_EXHAUSTED": FailureKind.POLICY,
    "REVIEW_PRICING_EXPIRED": FailureKind.POLICY,
    "REPORT_REMAINING_ALLOWANCE": FailureKind.POLICY,
    # The model kept repeating itself.
    "CODING_NO_NEW_EVIDENCE": FailureKind.NO_PROGRESS,
    "CODING_REPAIR_UNABLE": FailureKind.NO_PROGRESS,
    "REVIEW_NO_NEW_EVIDENCE": FailureKind.NO_PROGRESS,
    "REVIEW_REPEATING": FailureKind.NO_PROGRESS,
    # Checks could not run, or the harness was misconfigured.
    "CODING_REQUIRED_CHECK_CIRCUIT_OPEN": FailureKind.ENVIRONMENT,
    "CODING_AUTOMATIC_CHECK_BLOCKED": FailureKind.ENVIRONMENT,
    "MODEL_REQUEST_INVALID": FailureKind.ENVIRONMENT,
    "MODEL_PROMPT_TEMPLATE_MISMATCH": FailureKind.ENVIRONMENT,
    "MODEL_TOOL_FILTER_INVALID": FailureKind.ENVIRONMENT,
    # A completed review found a defect in the patch: a verification outcome.
    "REVIEW_CHANGES_REQUESTED": FailureKind.VERIFICATION,
}

_PREFIX_KINDS: Final = (
    (("REPAIR_", "MODEL_TOOL_", "MODEL_ACTION_", "REVIEW_PROTOCOL_"), FailureKind.PROTOCOL),
    (("CODING_STALLED", "CODING_HYPOTHESIS"), FailureKind.HYPOTHESIS),
    (("AUTHORITY_", "PERMISSION_", "POLICY_"), FailureKind.AUTHORITY),
    (("REVIEW_CONTEXT_",), FailureKind.CONTEXT),
)


def reason_kind(reason_code: str) -> FailureKind:
    if reason_code in REASON_KINDS:
        return REASON_KINDS[reason_code]
    for prefixes, kind in _PREFIX_KINDS:
        if reason_code.startswith(prefixes):
            return kind
    return FailureKind.UNKNOWN


def worker_failure(events: Sequence[DomainEvent]) -> FailureKind:
    for event in reversed(events):
        if isinstance(event, RunStopped):
            if event.reason == StopReason.STALLED:
                return FailureKind.NO_PROGRESS
            if event.reason in {StopReason.BUDGET_EXHAUSTED, StopReason.MAX_ITERATIONS}:
                return FailureKind.POLICY
        if isinstance(event, ModelAttemptFailed):
            failure = reason_kind(event.reason_code)
            if (
                failure in {FailureKind.UNKNOWN, FailureKind.PROVIDER}
                and event.context_admission is not None
                and not event.context_admission.admitted
            ):
                failure = FailureKind.CONTEXT
            return failure
        if isinstance(event, VerificationFailed):
            return FailureKind.ENVIRONMENT if event.inconclusive else FailureKind.VERIFICATION
    return FailureKind.UNKNOWN
