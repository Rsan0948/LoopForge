from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from loopforge.adapters.scripted import FixedClock
from loopforge.adapters.telemetry import (
    InMemoryTelemetry,
    NoOpTelemetry,
    TelemetrySandbox,
    to_otlp_log,
    to_otlp_metric,
    to_otlp_span,
)
from loopforge.application.telemetry import NullTelemetry, RuntimeTelemetry
from loopforge.domain.context_lifecycle import (
    AccountingEntry,
    ContextAccounting,
    ContextTokenBudget,
    DropReason,
)
from loopforge.domain.security import SandboxCapabilities
from loopforge.domain.telemetry import (
    REDACTION_PLACEHOLDER,
    CorrelationIds,
    LogRecord,
    LogSeverity,
    MetricKind,
    MetricName,
    MetricSample,
    SensitiveText,
    Span,
    SpanKind,
    SpanName,
    SpanStatusCode,
    correlation_attributes,
    redact_attributes,
    redact_record,
    redact_text,
)
from loopforge.domain.tooling import DataSensitivity
from loopforge.domain.types import (
    ActionId,
    ContextItemId,
    RunId,
    StopReason,
    VerificationId,
    WorkerId,
)
from loopforge.ports.sandbox import SandboxCommandResult, SandboxError
from loopforge.ports.telemetry import FailSafeTelemetry, TelemetryEmissionError

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(seconds=5)
RUN_ID = RunId("run_test000001")
NAIVE = datetime(2026, 8, 27, 12, 0)  # noqa: DTZ001 - intentionally naive to pin tz-awareness validation


def _correlation(**overrides: object) -> CorrelationIds:
    return CorrelationIds(run_id=RUN_ID, **overrides)  # pyright: ignore[reportArgumentType]


def _span(**overrides: object) -> Span:
    base: dict[str, object] = {
        "name": "loopforge.run",
        "trace_id": str(RUN_ID),
        "span_id": f"{RUN_ID}:span:0",
        "started_at": NOW,
        "ended_at": LATER,
        "correlation": _correlation(),
    }
    return Span(**(base | overrides))  # pyright: ignore[reportArgumentType]


def _log(**overrides: object) -> LogRecord:
    base: dict[str, object] = {
        "message": "something happened",
        "occurred_at": NOW,
        "correlation": _correlation(),
    }
    return LogRecord(**(base | overrides))  # pyright: ignore[reportArgumentType]


def _metric(**overrides: object) -> MetricSample:
    base: dict[str, object] = {
        "name": "loopforge.runs.started",
        "value": 1.0,
        "occurred_at": NOW,
        "correlation": _correlation(),
    }
    return MetricSample(**(base | overrides))  # pyright: ignore[reportArgumentType]


# --- vocabulary pins ---------------------------------------------------------


def test_span_kind_vocabulary_is_otel_compatible() -> None:
    assert {kind.value for kind in SpanKind} == {
        "internal",
        "server",
        "client",
        "producer",
        "consumer",
    }


def test_span_status_vocabulary_is_otel_compatible() -> None:
    assert {code.value for code in SpanStatusCode} == {"unset", "ok", "error"}


def test_log_severity_vocabulary() -> None:
    assert {severity.value for severity in LogSeverity} == {"debug", "info", "warn", "error"}


def test_metric_kind_vocabulary() -> None:
    assert {kind.value for kind in MetricKind} == {"counter", "gauge", "histogram"}


def test_span_name_vocabulary_covers_every_required_boundary() -> None:
    assert {name.value for name in SpanName} == {
        "loopforge.run",
        "loopforge.cycle",
        "loopforge.policy.decision",
        "loopforge.context.build",
        "loopforge.model.turn",
        "loopforge.tool.execute",
        "loopforge.retry",
        "loopforge.verify",
        "loopforge.persist",
        "loopforge.sandbox.execute",
    }


def test_metric_name_vocabulary_covers_every_required_metric() -> None:
    assert {name.value for name in MetricName} == {
        "loopforge.runs.started",
        "loopforge.runs.completed",
        "loopforge.run.duration_seconds",
        "loopforge.cycles",
        "loopforge.retries",
        "loopforge.circuits.opened",
        "loopforge.stalls",
        "loopforge.budget_stops",
        "loopforge.tool.failures",
        "loopforge.verification.failures",
        "loopforge.context.tokens.used",
        "loopforge.context.tokens.usable",
        "loopforge.context.items.kept",
        "loopforge.context.items.dropped",
        "loopforge.context.items.compacted",
        "loopforge.approvals.requested",
        "loopforge.approvals.granted",
        "loopforge.tokens.input",
        "loopforge.tokens.output",
        "loopforge.tokens.cached_input",
        "loopforge.cost.usd",
    }


# --- SensitiveText and redaction ----------------------------------------------


def test_sensitive_text_defaults_to_internal() -> None:
    assert SensitiveText("hello").sensitivity is DataSensitivity.INTERNAL


@pytest.mark.parametrize("sensitivity", [DataSensitivity.PUBLIC, DataSensitivity.INTERNAL])
def test_redact_text_passes_through_low_sensitivity(sensitivity: DataSensitivity) -> None:
    assert redact_text("visible", sensitivity) == "visible"


@pytest.mark.parametrize("sensitivity", [DataSensitivity.SENSITIVE, DataSensitivity.SECRET])
def test_redact_text_redacts_high_sensitivity(sensitivity: DataSensitivity) -> None:
    assert redact_text("hunter2", sensitivity) == REDACTION_PLACEHOLDER


def test_redact_attributes_redacts_only_sensitive_tagged_values() -> None:
    redacted = redact_attributes(
        {
            "plain": "value",
            "count": 3,
            "internal": SensitiveText("shown", DataSensitivity.INTERNAL),
            "secret": SensitiveText("hunter2", DataSensitivity.SECRET),
            "sensitive": SensitiveText("token-abc", DataSensitivity.SENSITIVE),
        }
    )
    assert redacted == {
        "plain": "value",
        "count": 3,
        "internal": "shown",
        "secret": REDACTION_PLACEHOLDER,
        "sensitive": REDACTION_PLACEHOLDER,
    }


def test_redact_record_returns_redacted_copy_without_mutating_original() -> None:
    record = _log(attributes={"key": SensitiveText("hunter2", DataSensitivity.SECRET)})
    redacted = redact_record(record)
    assert redacted is not record
    assert redacted.attributes == {"key": REDACTION_PLACEHOLDER}
    assert record.attributes["key"] == SensitiveText("hunter2", DataSensitivity.SECRET)


def test_redact_record_applies_to_spans_and_metrics() -> None:
    secret = SensitiveText("hunter2", DataSensitivity.SECRET)
    span = redact_record(_span(attributes={"k": secret}))
    metric = redact_record(_metric(attributes={"k": secret}))
    assert span.attributes == {"k": REDACTION_PLACEHOLDER}
    assert metric.attributes == {"k": REDACTION_PLACEHOLDER}


# --- CorrelationIds -------------------------------------------------------------


def test_correlation_ids_rejects_non_positive_cycle() -> None:
    with pytest.raises(ValueError, match="correlation cycle must be positive"):
        _correlation(cycle=0)


def test_correlation_ids_rejects_non_positive_attempt() -> None:
    with pytest.raises(ValueError, match="correlation attempt must be positive"):
        _correlation(attempt=0)


def test_correlation_ids_minimal_construction() -> None:
    correlation = _correlation()
    assert correlation.worker_id is None
    assert correlation.cycle is None
    assert correlation.action_id is None
    assert correlation.verification_id is None


def test_correlation_attributes_flattens_only_populated_fields() -> None:
    correlation = _correlation(
        worker_id=WorkerId("worker-1"),
        cycle=2,
        action_id=ActionId("a1"),
        tool_name="inspect",
        attempt=3,
        verification_id=VerificationId("v1"),
    )
    assert correlation_attributes(correlation) == {
        "loopforge.run_id": str(RUN_ID),
        "loopforge.worker_id": "worker-1",
        "loopforge.cycle": 2,
        "loopforge.action_id": "a1",
        "loopforge.tool_name": "inspect",
        "loopforge.attempt": 3,
        "loopforge.verification_id": "v1",
    }


def test_correlation_attributes_minimal() -> None:
    assert correlation_attributes(_correlation()) == {"loopforge.run_id": str(RUN_ID)}


# --- record validation ---------------------------------------------------------


def test_span_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="span name cannot be empty"):
        _span(name="  ")


def test_span_rejects_empty_trace_id() -> None:
    with pytest.raises(ValueError, match="span trace_id cannot be empty"):
        _span(trace_id="")


def test_span_rejects_empty_span_id() -> None:
    with pytest.raises(ValueError, match="span span_id cannot be empty"):
        _span(span_id=" ")


def test_span_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="span started_at must be timezone-aware"):
        _span(started_at=NAIVE)
    with pytest.raises(ValueError, match="span ended_at must be timezone-aware"):
        _span(ended_at=NAIVE)


def test_span_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="span ended_at cannot precede started_at"):
        _span(started_at=LATER, ended_at=NOW)


def test_span_defaults() -> None:
    span = _span()
    assert span.kind is SpanKind.INTERNAL
    assert span.status is SpanStatusCode.UNSET
    assert span.parent_span_id is None
    assert dict(span.attributes) == {}


def test_log_rejects_empty_message() -> None:
    with pytest.raises(ValueError, match="log message cannot be empty"):
        _log(message="")


def test_log_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="log occurred_at must be timezone-aware"):
        _log(occurred_at=NAIVE)


def test_log_defaults() -> None:
    record = _log()
    assert record.severity is LogSeverity.INFO
    assert record.trace_id is None
    assert record.span_id is None


def test_metric_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="metric name cannot be empty"):
        _metric(name="")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_metric_rejects_non_finite_value(value: float) -> None:
    with pytest.raises(ValueError, match="metric value must be finite"):
        _metric(value=value)


def test_metric_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="metric occurred_at must be timezone-aware"):
        _metric(occurred_at=NAIVE)


def test_metric_defaults() -> None:
    sample = _metric()
    assert sample.kind is MetricKind.COUNTER
    assert sample.unit == "1"


# --- FailSafeTelemetry ----------------------------------------------------------


class _ThrowingSink:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.attempts = 0

    def emit(self, record: object) -> None:
        del record
        self.attempts += 1
        raise self._error


def test_fail_safe_boundary_redacts_before_delegating() -> None:
    sink = InMemoryTelemetry()
    boundary = FailSafeTelemetry(sink)
    boundary.emit(_log(attributes={"k": SensitiveText("hunter2", DataSensitivity.SECRET)}))
    assert sink.logs[0].attributes == {"k": REDACTION_PLACEHOLDER}


@pytest.mark.parametrize(
    "error",
    [TelemetryEmissionError("exporter down"), RuntimeError("boom"), ValueError("bad")],
)
def test_fail_safe_boundary_swallows_adapter_failures(error: Exception) -> None:
    throwing = _ThrowingSink(error)
    boundary = FailSafeTelemetry(throwing)
    boundary.emit(_log())
    boundary.emit(_log())
    assert throwing.attempts == 2
    assert boundary.dropped_records == 2


def test_fail_safe_boundary_counts_failures_without_blocking_healthy_records() -> None:
    sink = InMemoryTelemetry()
    boundary = FailSafeTelemetry(sink)
    boundary.emit(_log(message="kept"))
    assert len(sink.records) == 1
    assert boundary.dropped_records == 0


# --- RuntimeTelemetry span machinery --------------------------------------------


def test_null_telemetry_is_a_silent_sink() -> None:
    NullTelemetry().emit(_log())


def test_trace_and_root_span_ids_derive_from_run_id() -> None:
    telemetry = RuntimeTelemetry(None, FixedClock(NOW))
    assert telemetry.trace_id_for(RUN_ID) == str(RUN_ID)
    assert telemetry.root_span_id_for(RUN_ID) == f"{RUN_ID}:span:0"


def test_span_parentage_follows_the_call_stack() -> None:
    sink = InMemoryTelemetry()
    telemetry = RuntimeTelemetry(sink, FixedClock(NOW))
    with (
        telemetry.span(RUN_ID, name=SpanName.CYCLE),
        telemetry.span(RUN_ID, name=SpanName.CONTEXT_BUILD),
    ):
        pass
    spans = {span.name: span for span in sink.spans}
    cycle = spans[SpanName.CYCLE.value]
    build = spans[SpanName.CONTEXT_BUILD.value]
    assert cycle.parent_span_id == telemetry.root_span_id_for(RUN_ID)
    assert build.parent_span_id == cycle.span_id
    assert all(span.trace_id == str(RUN_ID) for span in sink.spans)


def test_span_ids_are_deterministic_per_run() -> None:
    sink = InMemoryTelemetry()
    telemetry = RuntimeTelemetry(sink, FixedClock(NOW))
    with (
        telemetry.span(RUN_ID, name=SpanName.CYCLE),
        telemetry.span(RUN_ID, name=SpanName.CONTEXT_BUILD),
    ):
        pass
    ids = {span.span_id for span in sink.spans}
    assert ids == {f"{RUN_ID}:span:1", f"{RUN_ID}:span:2"}


def _boom() -> None:
    msg = "boom"
    raise RuntimeError(msg)


def test_span_status_is_error_when_the_operation_raises() -> None:
    sink = InMemoryTelemetry()
    telemetry = RuntimeTelemetry(sink, FixedClock(NOW))
    with (
        pytest.raises(RuntimeError, match="boom"),
        telemetry.span(RUN_ID, name=SpanName.MODEL_TURN),
    ):
        _boom()
    assert sink.spans[0].status is SpanStatusCode.ERROR


def test_span_handle_mutations_are_visible_in_the_emitted_span() -> None:
    sink = InMemoryTelemetry()
    telemetry = RuntimeTelemetry(sink, FixedClock(NOW))
    with telemetry.span(RUN_ID, name=SpanName.TOOL_EXECUTE) as handle:
        handle.attributes["loopforge.tool.ok"] = False
        handle.status = SpanStatusCode.ERROR
    span = sink.spans[0]
    assert span.attributes["loopforge.tool.ok"] is False
    assert span.status is SpanStatusCode.ERROR


def test_span_carries_cycle_and_operation_correlation() -> None:
    sink = InMemoryTelemetry()
    telemetry = RuntimeTelemetry(sink, FixedClock(NOW))
    telemetry.set_cycle(2)
    with telemetry.span(
        RUN_ID,
        name=SpanName.TOOL_EXECUTE,
        action_id=ActionId("a1"),
        tool_name="inspect",
        attempt=1,
    ):
        pass
    correlation = sink.spans[0].correlation
    assert correlation.cycle == 2
    assert correlation.action_id == ActionId("a1")
    assert correlation.tool_name == "inspect"
    assert correlation.attempt == 1


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("success_verified", SpanStatusCode.OK),
        ("cancelled", SpanStatusCode.UNSET),
        ("failure", SpanStatusCode.ERROR),
        ("stalled", SpanStatusCode.ERROR),
        ("budget_exhausted", SpanStatusCode.ERROR),
        ("max_iterations", SpanStatusCode.ERROR),
    ],
)
def test_run_span_status_reflects_stop_reason(reason: str, expected: SpanStatusCode) -> None:
    sink = InMemoryTelemetry()
    telemetry = RuntimeTelemetry(sink, FixedClock(NOW))
    telemetry.emit_run_span(
        RUN_ID, started_at=NOW, reason=StopReason(reason), attributes={"k": "v"}
    )
    span = sink.spans[0]
    assert span.span_id == telemetry.root_span_id_for(RUN_ID)
    assert span.parent_span_id is None
    assert span.status is expected
    assert span.name == SpanName.RUN.value
    assert span.started_at == NOW


def test_dropped_records_reflects_fail_safe_boundary() -> None:
    boundary_sink = _ThrowingSink(TelemetryEmissionError("down"))
    telemetry = RuntimeTelemetry(boundary_sink, FixedClock(NOW))
    with telemetry.span(RUN_ID, name=SpanName.CYCLE):
        pass
    assert telemetry.dropped_records == 1


# --- emit_context_accounting -----------------------------------------------------


def _accounting_builder(*, kept: int, dropped: int, compacted: int, overhead: int = 10) -> object:
    entries = tuple(
        AccountingEntry(
            item_id=ContextItemId(f"kept-{index}"),
            tokens=5,
            kept=True,
            compacted=index < compacted,
        )
        for index in range(kept)
    ) + tuple(
        AccountingEntry(
            item_id=ContextItemId(f"dropped-{index}"),
            tokens=0,
            kept=False,
            drop_reason=DropReason.OVER_BUDGET,
        )
        for index in range(dropped)
    )
    accounting = ContextAccounting(
        budget=ContextTokenBudget(max_tokens=1000, reserve_tokens=100),
        entries=entries,
        overhead_tokens=overhead,
    )

    class _Builder:
        @property
        def last_accounting(self) -> ContextAccounting:
            return accounting

    return _Builder()


def test_context_accounting_metrics_cover_size_and_compaction() -> None:
    sink = InMemoryTelemetry()
    telemetry = RuntimeTelemetry(sink, FixedClock(NOW))
    telemetry.set_cycle(1)
    telemetry.emit_context_accounting(RUN_ID, _accounting_builder(kept=3, dropped=2, compacted=1))
    samples = {sample.name: sample for sample in sink.metrics}
    assert samples[MetricName.CONTEXT_TOKENS_USED.value].value == 25.0
    assert samples[MetricName.CONTEXT_TOKENS_USED.value].kind is MetricKind.GAUGE
    assert samples[MetricName.CONTEXT_TOKENS_USED.value].unit == "token"
    assert samples[MetricName.CONTEXT_TOKENS_USABLE.value].value == 900.0
    assert samples[MetricName.CONTEXT_ITEMS_KEPT.value].value == 3.0
    assert samples[MetricName.CONTEXT_ITEMS_DROPPED.value].value == 2.0
    assert samples[MetricName.CONTEXT_ITEMS_COMPACTED.value].value == 1.0
    assert samples[MetricName.CONTEXT_ITEMS_KEPT.value].kind is MetricKind.COUNTER
    assert all(sample.correlation.cycle == 1 for sample in sink.metrics)


def test_context_accounting_skips_builders_without_the_seam() -> None:
    sink = InMemoryTelemetry()
    telemetry = RuntimeTelemetry(sink, FixedClock(NOW))
    telemetry.emit_context_accounting(RUN_ID, object())
    assert sink.records == ()


def test_context_accounting_skips_empty_ledgers() -> None:
    class _EmptyBuilder:
        @property
        def last_accounting(self) -> None:
            return None

    sink = InMemoryTelemetry()
    telemetry = RuntimeTelemetry(sink, FixedClock(NOW))
    telemetry.emit_context_accounting(RUN_ID, _EmptyBuilder())
    assert sink.records == ()


# --- adapters --------------------------------------------------------------------


def test_no_op_telemetry_discards_records() -> None:
    NoOpTelemetry().emit(_span())


def test_in_memory_telemetry_filters_by_record_kind() -> None:
    sink = InMemoryTelemetry()
    sink.emit(_span())
    sink.emit(_log())
    sink.emit(_metric())
    assert len(sink.records) == 3
    assert len(sink.spans) == 1
    assert len(sink.logs) == 1
    assert len(sink.metrics) == 1


def test_otlp_span_shape_is_wire_compatible() -> None:
    child = _span(
        name=SpanName.CONTEXT_BUILD.value,
        span_id=f"{RUN_ID}:span:2",
        parent_span_id=f"{RUN_ID}:span:1",
        kind=SpanKind.CLIENT,
        status=SpanStatusCode.ERROR,
        attributes={"count": 3, "ratio": 0.5, "flag": True, "label": "x"},
    )
    otlp = to_otlp_span(child)
    assert otlp["name"] == "loopforge.context.build"
    assert len(str(otlp["traceId"])) == 32
    assert len(str(otlp["spanId"])) == 16
    assert len(str(otlp["parentSpanId"])) == 16
    assert otlp["kind"] == 3  # CLIENT
    assert otlp["status"] == {"code": 2}  # ERROR
    assert str(otlp["startTimeUnixNano"]).isdigit()
    attribute_items = cast("list[dict[str, object]]", otlp["attributes"])
    attributes = {item["key"]: item["value"] for item in attribute_items}
    assert attributes["count"] == {"intValue": "3"}
    assert attributes["ratio"] == {"doubleValue": 0.5}
    assert attributes["flag"] == {"boolValue": True}
    assert attributes["label"] == {"stringValue": "x"}
    assert attributes["loopforge.run_id"] == {"stringValue": str(RUN_ID)}


def test_otlp_root_span_has_empty_parent() -> None:
    otlp = to_otlp_span(_span())
    assert otlp["parentSpanId"] == ""
    assert otlp["kind"] == 1  # INTERNAL
    assert otlp["status"] == {"code": 0}  # UNSET


def test_otlp_ids_are_deterministic() -> None:
    assert to_otlp_span(_span())["traceId"] == to_otlp_span(_span())["traceId"]


def test_otlp_metric_counter_shape() -> None:
    otlp = to_otlp_metric(_metric(name="loopforge.retries", value=1.0))
    assert otlp["name"] == "loopforge.retries"
    assert otlp["unit"] == "1"
    aggregated = cast("dict[str, object]", otlp["sum"])
    assert aggregated["isMonotonic"] is True
    assert aggregated["aggregationTemporality"] == 2
    data_points = cast("list[dict[str, object]]", aggregated["dataPoints"])
    assert data_points[0]["asInt"] == "1"


def test_otlp_metric_gauge_and_fractional_value() -> None:
    otlp = to_otlp_metric(_metric(value=0.25, kind=MetricKind.GAUGE, unit="token"))
    gauge = cast("dict[str, object]", otlp["gauge"])
    data_point = cast("list[dict[str, object]]", gauge["dataPoints"])[0]
    assert data_point["asDouble"] == 0.25


def test_otlp_metric_histogram_shape() -> None:
    otlp = to_otlp_metric(_metric(value=3.0, kind=MetricKind.HISTOGRAM, unit="s"))
    histogram = cast("dict[str, object]", otlp["histogram"])
    assert histogram["aggregationTemporality"] == 2
    data_point = cast("list[dict[str, object]]", histogram["dataPoints"])[0]
    assert data_point["count"] == "1"
    assert data_point["sum"] == 3.0


def test_otlp_log_shape() -> None:
    record = _log(
        severity=LogSeverity.WARN,
        trace_id=str(RUN_ID),
        span_id=f"{RUN_ID}:span:1",
    )
    otlp = to_otlp_log(record)
    assert otlp["severityText"] == "WARN"
    assert otlp["body"] == {"stringValue": "something happened"}
    assert len(str(otlp["traceId"])) == 32
    assert len(str(otlp["spanId"])) == 16


def test_otlp_log_without_trace_context() -> None:
    otlp = to_otlp_log(_log())
    assert otlp["traceId"] == ""
    assert otlp["spanId"] == ""


def test_otlp_conversion_rejects_unredacted_sensitive_text() -> None:
    record = _log(attributes={"k": SensitiveText("hunter2", DataSensitivity.SECRET)})
    with pytest.raises(ValueError, match="must be redacted primitives before export"):
        to_otlp_log(record)


# --- TelemetrySandbox -------------------------------------------------------------


class _FakeSandbox:
    def __init__(self, *, error: Exception | None = None, exit_code: int = 0) -> None:
        self._error = error
        self._exit_code = exit_code
        self.writes: list[tuple[str, str]] = []

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            file_api_confined=True,
            symlink_protected=True,
            environment_filtered=True,
            process_timeout=True,
            resource_limits=True,
            output_limited=True,
            process_filesystem_isolated=False,
            network_isolated=False,
            kernel_isolated=False,
        )

    def read_text(self, relative_path: str) -> str:
        if self._error is not None:
            raise self._error
        return f"content of {relative_path}"

    def write_text(self, relative_path: str, content: str) -> None:
        if self._error is not None:
            raise self._error
        self.writes.append((relative_path, content))

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        del command_name, timeout_seconds
        if self._error is not None:
            raise self._error
        return SandboxCommandResult(
            exit_code=self._exit_code,
            stdout="out",
            stderr="",
            succeeded=self._exit_code == 0,
        )


def test_sandbox_wrapper_delegates_capabilities() -> None:
    sandbox = TelemetrySandbox(_FakeSandbox(), InMemoryTelemetry(), clock=FixedClock(NOW))
    assert sandbox.capabilities.resource_limits is True


def test_sandbox_run_emits_ok_span_with_exit_code() -> None:
    sink = InMemoryTelemetry()
    sandbox = TelemetrySandbox(_FakeSandbox(), sink, clock=FixedClock(NOW))
    result = sandbox.run("pytest", timeout_seconds=2.0)
    assert result.succeeded is True
    span = sink.spans[0]
    assert span.name == "loopforge.sandbox.execute"
    assert span.status is SpanStatusCode.OK
    assert span.attributes["loopforge.sandbox.command"] == "pytest"
    assert span.attributes["loopforge.sandbox.exit_code"] == 0
    assert span.attributes["loopforge.sandbox.timeout_seconds"] == 2.0


def test_sandbox_run_marks_nonzero_exit_as_error() -> None:
    sink = InMemoryTelemetry()
    sandbox = TelemetrySandbox(_FakeSandbox(exit_code=3), sink, clock=FixedClock(NOW))
    result = sandbox.run("pytest")
    assert result.succeeded is False
    assert sink.spans[0].status is SpanStatusCode.ERROR
    assert "loopforge.sandbox.timeout_seconds" not in sink.spans[0].attributes


def test_sandbox_failures_emit_error_spans_and_reraise() -> None:
    sink = InMemoryTelemetry()
    failing = _FakeSandbox(error=SandboxError("denied"))
    sandbox = TelemetrySandbox(failing, sink, clock=FixedClock(NOW))
    with pytest.raises(SandboxError, match="denied"):
        sandbox.read_text("src/x.py")
    with pytest.raises(SandboxError, match="denied"):
        sandbox.write_text("src/x.py", "data")
    with pytest.raises(SandboxError, match="denied"):
        sandbox.run("pytest")
    assert [span.status for span in sink.spans] == [SpanStatusCode.ERROR] * 3
    assert {span.attributes["loopforge.sandbox.operation"] for span in sink.spans} == {
        "read_text",
        "write_text",
        "run",
    }


def test_sandbox_read_and_write_emit_ok_spans() -> None:
    sink = InMemoryTelemetry()
    sandbox = TelemetrySandbox(_FakeSandbox(), sink, clock=FixedClock(NOW))
    assert sandbox.read_text("a.py") == "content of a.py"
    sandbox.write_text("a.py", "code")
    assert [span.status for span in sink.spans] == [SpanStatusCode.OK, SpanStatusCode.OK]


def test_sandbox_span_ids_are_deterministic_and_join_run_trace() -> None:
    sink = InMemoryTelemetry()
    correlation = _correlation()
    sandbox = TelemetrySandbox(_FakeSandbox(), sink, clock=FixedClock(NOW), correlation=correlation)
    sandbox.run("pytest")
    sandbox.run("ruff")
    first, second = sink.spans
    assert first.trace_id == str(RUN_ID)
    assert first.span_id == f"{RUN_ID}:sandbox:1"
    assert second.span_id == f"{RUN_ID}:sandbox:2"
    assert first.correlation is correlation


def test_sandbox_standalone_correlation_is_documented_default() -> None:
    sink = InMemoryTelemetry()
    sandbox = TelemetrySandbox(_FakeSandbox(), sink, clock=FixedClock(NOW))
    sandbox.run("pytest")
    assert sink.spans[0].correlation.run_id == RunId("sandbox.standalone")


def test_sandbox_emission_is_fail_safe() -> None:
    sandbox = TelemetrySandbox(
        _FakeSandbox(),
        _ThrowingSink(TelemetryEmissionError("down")),
        clock=FixedClock(NOW),
    )
    assert sandbox.run("pytest").succeeded is True
