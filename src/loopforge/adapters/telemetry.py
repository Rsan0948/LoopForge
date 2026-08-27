from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime

from loopforge.domain.security import SandboxCapabilities
from loopforge.domain.telemetry import (
    CorrelationIds,
    LogRecord,
    MetricKind,
    MetricSample,
    SensitiveText,
    Span,
    SpanKind,
    SpanStatusCode,
    TelemetryAttributes,
    TelemetryAttributeValue,
    TelemetryRecord,
    correlation_attributes,
)
from loopforge.domain.types import RunId
from loopforge.ports.clock import ClockPort
from loopforge.ports.sandbox import SandboxCommandResult, SandboxPort
from loopforge.ports.telemetry import FailSafeTelemetry, TelemetryPort


class NoOpTelemetry:
    """Production no-op sink for wiring telemetry off explicitly."""

    def emit(self, record: TelemetryRecord) -> None:
        del record


class InMemoryTelemetry:
    """Deterministic recording sink for tests and offline demos.

    Keeps every emitted record in process memory; no network access, no
    exporter, no collector. Records captured here have already passed the
    fail-safe redaction boundary when emitted through the runtime.
    """

    def __init__(self) -> None:
        self._records: list[TelemetryRecord] = []

    def emit(self, record: TelemetryRecord) -> None:
        self._records.append(record)

    @property
    def records(self) -> tuple[TelemetryRecord, ...]:
        return tuple(self._records)

    @property
    def spans(self) -> tuple[Span, ...]:
        return tuple(record for record in self._records if isinstance(record, Span))

    @property
    def logs(self) -> tuple[LogRecord, ...]:
        return tuple(record for record in self._records if isinstance(record, LogRecord))

    @property
    def metrics(self) -> tuple[MetricSample, ...]:
        return tuple(record for record in self._records if isinstance(record, MetricSample))


# ---------------------------------------------------------------------------
# OpenTelemetry-compatible (OTLP/JSON-shaped) conversion.
#
# Compatibility is proven through the data model: these converters map the
# domain telemetry vocabulary onto the OTLP/JSON wire shapes (hex trace/span
# ids, nanosecond timestamps, kind/status codes, typed attribute values)
# without any SDK, network access, or live collector.
# ---------------------------------------------------------------------------

_OTLP_SPAN_KIND_CODES: Mapping[SpanKind, int] = {
    SpanKind.INTERNAL: 1,
    SpanKind.SERVER: 2,
    SpanKind.CLIENT: 3,
    SpanKind.PRODUCER: 4,
    SpanKind.CONSUMER: 5,
}

_OTLP_STATUS_CODES: Mapping[SpanStatusCode, int] = {
    SpanStatusCode.UNSET: 0,
    SpanStatusCode.OK: 1,
    SpanStatusCode.ERROR: 2,
}

_OTLP_AGGREGATION_TEMPORALITY_CUMULATIVE = 2


def _otel_trace_id(trace_id: str) -> str:
    return hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:32]


def _otel_span_id(span_id: str) -> str:
    return hashlib.sha256(f"span:{span_id}".encode()).hexdigest()[:16]


def _unix_nanos(value: datetime) -> str:
    return str(int(value.timestamp() * 1_000_000_000))


def _otel_value(value: TelemetryAttributeValue | SensitiveText) -> dict[str, object]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    msg = (
        "telemetry attribute values must be redacted primitives before export, "
        f"got {type(value).__name__}"
    )
    raise ValueError(msg)


def _otel_attributes(
    attributes: TelemetryAttributes, correlation: CorrelationIds
) -> list[dict[str, object]]:
    merged: dict[str, TelemetryAttributeValue | SensitiveText] = {
        **correlation_attributes(correlation),
        **attributes,
    }
    return [{"key": key, "value": _otel_value(value)} for key, value in merged.items()]


def to_otlp_span(span: Span) -> dict[str, object]:
    """Map a domain span onto the OTLP/JSON span shape."""
    return {
        "name": span.name,
        "traceId": _otel_trace_id(span.trace_id),
        "spanId": _otel_span_id(span.span_id),
        "parentSpanId": _otel_span_id(span.parent_span_id) if span.parent_span_id else "",
        "kind": _OTLP_SPAN_KIND_CODES[span.kind],
        "startTimeUnixNano": _unix_nanos(span.started_at),
        "endTimeUnixNano": _unix_nanos(span.ended_at),
        "status": {"code": _OTLP_STATUS_CODES[span.status]},
        "attributes": _otel_attributes(span.attributes, span.correlation),
    }


def to_otlp_metric(sample: MetricSample) -> dict[str, object]:
    """Map a domain metric sample onto the OTLP/JSON metric shape."""
    data_point: dict[str, object] = {
        "timeUnixNano": _unix_nanos(sample.occurred_at),
        "attributes": _otel_attributes(sample.attributes, sample.correlation),
    }
    if sample.value.is_integer():
        data_point["asInt"] = str(int(sample.value))
    else:
        data_point["asDouble"] = sample.value
    metric: dict[str, object] = {"name": sample.name, "unit": sample.unit}
    if sample.kind is MetricKind.COUNTER:
        metric["sum"] = {
            "dataPoints": [data_point],
            "aggregationTemporality": _OTLP_AGGREGATION_TEMPORALITY_CUMULATIVE,
            "isMonotonic": True,
        }
    elif sample.kind is MetricKind.GAUGE:
        metric["gauge"] = {"dataPoints": [data_point]}
    else:
        histogram_point = {**data_point, "count": "1", "sum": sample.value}
        metric["histogram"] = {
            "dataPoints": [histogram_point],
            "aggregationTemporality": _OTLP_AGGREGATION_TEMPORALITY_CUMULATIVE,
        }
    return metric


def to_otlp_log(record: LogRecord) -> dict[str, object]:
    """Map a domain log record onto the OTLP/JSON log-record shape."""
    return {
        "timeUnixNano": _unix_nanos(record.occurred_at),
        "severityText": record.severity.value.upper(),
        "body": {"stringValue": record.message},
        "traceId": _otel_trace_id(record.trace_id) if record.trace_id else "",
        "spanId": _otel_span_id(record.span_id) if record.span_id else "",
        "attributes": _otel_attributes(record.attributes, record.correlation),
    }


_STANDALONE_CORRELATION = CorrelationIds(run_id=RunId("sandbox.standalone"))


class TelemetrySandbox:
    """SandboxPort decorator that emits sandbox execution spans.

    Span emission is fail-safe (a broken sink can never break the sandbox) and
    non-authoritative (spans are observability metadata only). When composed
    with a run's correlation ids the sandbox spans join the run trace;
    standalone use emits spans under a documented standalone correlation.
    """

    def __init__(
        self,
        sandbox: SandboxPort,
        telemetry: TelemetryPort,
        *,
        clock: ClockPort,
        correlation: CorrelationIds | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._sink = FailSafeTelemetry(telemetry)
        self._clock = clock
        self._correlation = correlation if correlation is not None else _STANDALONE_CORRELATION
        self._span_seq = 0

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self._sandbox.capabilities

    def _next_span_id(self) -> str:
        self._span_seq += 1
        return f"{self._correlation.run_id}:sandbox:{self._span_seq}"

    def _emit_span(
        self,
        *,
        started_at: datetime,
        status: SpanStatusCode,
        attributes: Mapping[str, TelemetryAttributeValue | SensitiveText],
    ) -> None:
        trace_id = str(self._correlation.run_id)
        self._sink.emit(
            Span(
                name="loopforge.sandbox.execute",
                trace_id=trace_id,
                span_id=self._next_span_id(),
                parent_span_id=None,
                started_at=started_at,
                ended_at=self._clock.now(),
                status=status,
                attributes=dict(attributes),
                correlation=self._correlation,
            )
        )

    def read_text(self, relative_path: str) -> str:
        attributes: dict[str, TelemetryAttributeValue] = {
            "loopforge.sandbox.operation": "read_text",
            "loopforge.sandbox.path": relative_path,
        }
        started_at = self._clock.now()
        try:
            result = self._sandbox.read_text(relative_path)
        except Exception:
            self._emit_span(
                started_at=started_at, status=SpanStatusCode.ERROR, attributes=attributes
            )
            raise
        self._emit_span(started_at=started_at, status=SpanStatusCode.OK, attributes=attributes)
        return result

    def write_text(self, relative_path: str, content: str) -> None:
        attributes: dict[str, TelemetryAttributeValue] = {
            "loopforge.sandbox.operation": "write_text",
            "loopforge.sandbox.path": relative_path,
        }
        started_at = self._clock.now()
        try:
            self._sandbox.write_text(relative_path, content)
        except Exception:
            self._emit_span(
                started_at=started_at, status=SpanStatusCode.ERROR, attributes=attributes
            )
            raise
        self._emit_span(started_at=started_at, status=SpanStatusCode.OK, attributes=attributes)

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        attributes: dict[str, TelemetryAttributeValue] = {
            "loopforge.sandbox.operation": "run",
            "loopforge.sandbox.command": command_name,
        }
        if timeout_seconds is not None:
            attributes["loopforge.sandbox.timeout_seconds"] = timeout_seconds
        started_at = self._clock.now()
        try:
            result = self._sandbox.run(command_name, timeout_seconds=timeout_seconds)
        except Exception:
            self._emit_span(
                started_at=started_at, status=SpanStatusCode.ERROR, attributes=attributes
            )
            raise
        attributes["loopforge.sandbox.exit_code"] = result.exit_code
        status = SpanStatusCode.OK if result.succeeded else SpanStatusCode.ERROR
        self._emit_span(started_at=started_at, status=status, attributes=attributes)
        return result
