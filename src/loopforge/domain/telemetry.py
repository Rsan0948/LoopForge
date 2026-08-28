from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

from loopforge.domain.tooling import DataSensitivity
from loopforge.domain.types import ActionId, RunId, VerificationId, WorkerId


class SpanKind(StrEnum):
    """OpenTelemetry-compatible span kinds."""

    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"


class SpanStatusCode(StrEnum):
    """OpenTelemetry-compatible span status codes."""

    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


class LogSeverity(StrEnum):
    """Severity vocabulary for structured telemetry log records."""

    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class MetricKind(StrEnum):
    """OpenTelemetry-compatible metric instrument kinds."""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class SpanName(StrEnum):
    """Closed vocabulary of runtime span names."""

    RUN = "loopforge.run"
    CYCLE = "loopforge.cycle"
    POLICY_DECISION = "loopforge.policy.decision"
    CONTEXT_BUILD = "loopforge.context.build"
    MODEL_TURN = "loopforge.model.turn"
    MODEL_ROUTE = "loopforge.model.route"
    TOOL_EXECUTE = "loopforge.tool.execute"
    RETRY = "loopforge.retry"
    VERIFY = "loopforge.verify"
    PERSIST = "loopforge.persist"
    SANDBOX_EXECUTE = "loopforge.sandbox.execute"


class MetricName(StrEnum):
    """Closed vocabulary of runtime metric names (OTel-style dotted names)."""

    RUNS_STARTED = "loopforge.runs.started"
    RUNS_COMPLETED = "loopforge.runs.completed"
    RUN_DURATION_SECONDS = "loopforge.run.duration_seconds"
    CYCLES = "loopforge.cycles"
    RETRIES = "loopforge.retries"
    CIRCUITS_OPENED = "loopforge.circuits.opened"
    STALLS = "loopforge.stalls"
    BUDGET_STOPS = "loopforge.budget_stops"
    TOOL_FAILURES = "loopforge.tool.failures"
    VERIFICATION_FAILURES = "loopforge.verification.failures"
    CONTEXT_TOKENS_USED = "loopforge.context.tokens.used"
    CONTEXT_TOKENS_USABLE = "loopforge.context.tokens.usable"
    CONTEXT_ITEMS_KEPT = "loopforge.context.items.kept"
    CONTEXT_ITEMS_DROPPED = "loopforge.context.items.dropped"
    CONTEXT_ITEMS_COMPACTED = "loopforge.context.items.compacted"
    APPROVALS_REQUESTED = "loopforge.approvals.requested"
    APPROVALS_GRANTED = "loopforge.approvals.granted"
    WORKERS_SPAWNED = "loopforge.workers.spawned"
    WORKERS_STOPPED = "loopforge.workers.stopped"
    WORKERS_MERGED = "loopforge.workers.merged"
    TOKENS_INPUT = "loopforge.tokens.input"
    TOKENS_OUTPUT = "loopforge.tokens.output"
    TOKENS_CACHED_INPUT = "loopforge.tokens.cached_input"
    COST_USD = "loopforge.cost.usd"


REDACTION_PLACEHOLDER: Final = "[redacted]"

# Sensitivities that must never cross the telemetry emission boundary in clear
# text. SECRET content is already barred from persistence (PACS-006); telemetry
# redacts SENSITIVE and SECRET before any record reaches an adapter.
REDACTED_SENSITIVITIES: Final = frozenset({DataSensitivity.SENSITIVE, DataSensitivity.SECRET})


@dataclass(frozen=True, slots=True)
class SensitiveText:
    """Free text tagged with the data sensitivity of its source.

    Emission sites wrap potentially sensitive strings (tool observations, error
    messages) so the fail-safe emission boundary can redact them before any
    record reaches a telemetry adapter. Redaction is code-owned here, never
    delegated to exporters.
    """

    text: str
    sensitivity: DataSensitivity = DataSensitivity.INTERNAL


TelemetryAttributeValue = str | int | float | bool
TelemetryAttributes = Mapping[str, TelemetryAttributeValue | SensitiveText]


def _new_attributes() -> TelemetryAttributes:
    return {}


def redact_text(text: str, sensitivity: DataSensitivity) -> str:
    """Return text safe for telemetry export at the given sensitivity."""
    if sensitivity in REDACTED_SENSITIVITIES:
        return REDACTION_PLACEHOLDER
    return text


def redact_attributes(attributes: TelemetryAttributes) -> dict[str, TelemetryAttributeValue]:
    """Replace sensitivity-tagged values with the redaction placeholder.

    PUBLIC and INTERNAL text passes through; SENSITIVE and SECRET text never
    leaves the runtime in a telemetry record.
    """
    redacted: dict[str, TelemetryAttributeValue] = {}
    for key, value in attributes.items():
        if isinstance(value, SensitiveText):
            redacted[key] = redact_text(value.text, value.sensitivity)
        else:
            redacted[key] = value
    return redacted


def _require_tz_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None:
        msg = f"{field_name} must be timezone-aware"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class CorrelationIds:
    """Causal correlation identifiers attached to every telemetry record.

    `worker_id` is reserved for multi-worker execution (PACS-013); the current
    single-worker runtime leaves it unset. Identifiers are observability
    metadata only: they are derived from authoritative runtime state and never
    feed back into runtime decisions.
    """

    run_id: RunId
    worker_id: WorkerId | None = None
    cycle: int | None = None
    action_id: ActionId | None = None
    tool_name: str | None = None
    attempt: int | None = None
    verification_id: VerificationId | None = None

    def __post_init__(self) -> None:
        if self.cycle is not None and self.cycle <= 0:
            msg = "correlation cycle must be positive"
            raise ValueError(msg)
        if self.attempt is not None and self.attempt <= 0:
            msg_2 = "correlation attempt must be positive"
            raise ValueError(msg_2)


def correlation_attributes(correlation: CorrelationIds) -> dict[str, TelemetryAttributeValue]:
    """Flatten correlation identifiers into OTel-style attribute key/value pairs."""
    attributes: dict[str, TelemetryAttributeValue] = {"loopforge.run_id": str(correlation.run_id)}
    if correlation.worker_id is not None:
        attributes["loopforge.worker_id"] = str(correlation.worker_id)
    if correlation.cycle is not None:
        attributes["loopforge.cycle"] = correlation.cycle
    if correlation.action_id is not None:
        attributes["loopforge.action_id"] = str(correlation.action_id)
    if correlation.tool_name is not None:
        attributes["loopforge.tool_name"] = correlation.tool_name
    if correlation.attempt is not None:
        attributes["loopforge.attempt"] = correlation.attempt
    if correlation.verification_id is not None:
        attributes["loopforge.verification_id"] = str(correlation.verification_id)
    return attributes


@dataclass(frozen=True, slots=True, kw_only=True)
class Span:
    """An OpenTelemetry-compatible trace span (data model only, no SDK)."""

    name: str
    trace_id: str
    span_id: str
    started_at: datetime
    ended_at: datetime
    correlation: CorrelationIds
    parent_span_id: str | None = None
    kind: SpanKind = SpanKind.INTERNAL
    status: SpanStatusCode = SpanStatusCode.UNSET
    attributes: TelemetryAttributes = field(default_factory=_new_attributes)

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "span name cannot be empty"
            raise ValueError(msg)
        if not self.trace_id.strip():
            msg_2 = "span trace_id cannot be empty"
            raise ValueError(msg_2)
        if not self.span_id.strip():
            msg_3 = "span span_id cannot be empty"
            raise ValueError(msg_3)
        _require_tz_aware(self.started_at, "span started_at")
        _require_tz_aware(self.ended_at, "span ended_at")
        if self.ended_at < self.started_at:
            msg_4 = "span ended_at cannot precede started_at"
            raise ValueError(msg_4)


@dataclass(frozen=True, slots=True, kw_only=True)
class LogRecord:
    """A structured log record correlated to the run trace."""

    message: str
    occurred_at: datetime
    correlation: CorrelationIds
    severity: LogSeverity = LogSeverity.INFO
    trace_id: str | None = None
    span_id: str | None = None
    attributes: TelemetryAttributes = field(default_factory=_new_attributes)

    def __post_init__(self) -> None:
        if not self.message.strip():
            msg = "log message cannot be empty"
            raise ValueError(msg)
        _require_tz_aware(self.occurred_at, "log occurred_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricSample:
    """An OpenTelemetry-compatible metric data point (data model only)."""

    name: str
    value: float
    occurred_at: datetime
    correlation: CorrelationIds
    kind: MetricKind = MetricKind.COUNTER
    unit: str = "1"
    attributes: TelemetryAttributes = field(default_factory=_new_attributes)

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "metric name cannot be empty"
            raise ValueError(msg)
        if not math.isfinite(self.value):
            msg_2 = "metric value must be finite"
            raise ValueError(msg_2)
        _require_tz_aware(self.occurred_at, "metric occurred_at")


TelemetryRecord = Span | LogRecord | MetricSample


def redact_record(record: TelemetryRecord) -> TelemetryRecord:
    """Return a copy of a record with all sensitive attributes redacted.

    This is the code-owned redaction step applied at the emission boundary
    before any telemetry adapter sees a record.
    """
    return replace(record, attributes=redact_attributes(record.attributes))
