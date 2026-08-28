from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    ActionRejected,
    ApprovalGranted,
    ApprovalRequested,
    ArtifactRecorded,
    BudgetDebited,
    CircuitOpened,
    ContextAssembled,
    Event,
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
)
from loopforge.domain.telemetry import (
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
    TelemetryAttributeValue,
)
from loopforge.domain.tooling import DataSensitivity
from loopforge.domain.types import ActionId, RunId, StopReason, VerificationId
from loopforge.ports.clock import ClockPort
from loopforge.ports.context import ContextAccountingSource
from loopforge.ports.telemetry import FailSafeTelemetry, TelemetryPort


class NullTelemetry:
    """Application-owned no-op sink used when no telemetry adapter is wired."""

    def emit(self, record: object) -> None:
        del record


@dataclass(slots=True)
class SpanHandle:
    """Mutable in-flight span state yielded by `RuntimeTelemetry.span`.

    Callers enrich attributes while the operation runs and may downgrade the
    status to ERROR for failures reported as values rather than exceptions.
    """

    attributes: dict[str, TelemetryAttributeValue | SensitiveText]
    status: SpanStatusCode = SpanStatusCode.OK


@dataclass(slots=True)
class _TraceState:
    """Per-run trace/projection state. Purely observational; never authoritative."""

    next_span_seq: int = 1  # span 0 is reserved for the run root span
    parent_stack: list[str] = field(default_factory=list[str])
    started_at: datetime | None = None
    action_tools: dict[str, str] = field(default_factory=dict[str, str])
    action_sensitivities: dict[str, DataSensitivity] = field(
        default_factory=dict[str, DataSensitivity]
    )


class RuntimeTelemetry:
    """Fail-safe, non-authoritative telemetry emission for the runtime.

    Telemetry is a projection of authoritative runtime activity: it reads
    domain events and operation boundaries but never feeds back into state
    transitions or decisions. All emission flows through the fail-safe
    boundary (redaction + exception containment).
    """

    def __init__(self, sink: TelemetryPort | None, clock: ClockPort) -> None:
        self._sink = FailSafeTelemetry(sink if sink is not None else NullTelemetry())
        self._clock = clock
        self._states: dict[RunId, _TraceState] = {}
        self._cycle: int | None = None

    @property
    def dropped_records(self) -> int:
        """Records swallowed by the fail-safe boundary after adapter failures."""
        return self._sink.dropped_records

    @staticmethod
    def trace_id_for(run_id: RunId) -> str:
        return str(run_id)

    @staticmethod
    def root_span_id_for(run_id: RunId) -> str:
        return f"{run_id}:span:0"

    def set_cycle(self, cycle: int | None) -> None:
        """Record the current drive-loop cycle for correlation of emitted records."""
        self._cycle = cycle

    def _state_for(self, run_id: RunId) -> _TraceState:
        if run_id not in self._states:
            self._states[run_id] = _TraceState()
        return self._states[run_id]

    def _correlation(
        self,
        run_id: RunId,
        *,
        action_id: ActionId | None = None,
        tool_name: str | None = None,
        attempt: int | None = None,
        verification_id: VerificationId | None = None,
    ) -> CorrelationIds:
        return CorrelationIds(
            run_id=run_id,
            cycle=self._cycle,
            action_id=action_id,
            tool_name=tool_name,
            attempt=attempt,
            verification_id=verification_id,
        )

    @contextmanager
    def span(  # noqa: PLR0913 - keyword-only correlation plumbing keeps every call site explicit
        self,
        run_id: RunId,
        *,
        name: SpanName,
        attributes: Mapping[str, TelemetryAttributeValue | SensitiveText] | None = None,
        action_id: ActionId | None = None,
        tool_name: str | None = None,
        attempt: int | None = None,
        verification_id: VerificationId | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
    ) -> Generator[SpanHandle, None, None]:
        """Emit a span around a runtime operation; parentage follows the call stack."""
        state = self._state_for(run_id)
        span_id = f"{run_id}:span:{state.next_span_seq}"
        state.next_span_seq += 1
        parent_span_id = (
            state.parent_stack[-1] if state.parent_stack else self.root_span_id_for(run_id)
        )
        handle = SpanHandle(attributes=dict(attributes) if attributes is not None else {})
        state.parent_stack.append(span_id)
        started_at = self._clock.now()
        try:
            yield handle
        except Exception:
            handle.status = SpanStatusCode.ERROR
            raise
        finally:
            state.parent_stack.pop()
            self._sink.emit(
                Span(
                    name=name.value,
                    trace_id=self.trace_id_for(run_id),
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    kind=kind,
                    started_at=started_at,
                    ended_at=self._clock.now(),
                    status=handle.status,
                    attributes=handle.attributes,
                    correlation=self._correlation(
                        run_id,
                        action_id=action_id,
                        tool_name=tool_name,
                        attempt=attempt,
                        verification_id=verification_id,
                    ),
                )
            )

    def emit_run_span(
        self,
        run_id: RunId,
        *,
        started_at: datetime,
        reason: StopReason,
        attributes: Mapping[str, TelemetryAttributeValue | SensitiveText],
    ) -> None:
        """Emit the root run span when a run reaches a terminal stop."""
        if reason is StopReason.SUCCESS_VERIFIED:
            status = SpanStatusCode.OK
        elif reason is StopReason.CANCELLED:
            status = SpanStatusCode.UNSET
        else:
            status = SpanStatusCode.ERROR
        self._sink.emit(
            Span(
                name=SpanName.RUN.value,
                trace_id=self.trace_id_for(run_id),
                span_id=self.root_span_id_for(run_id),
                parent_span_id=None,
                kind=SpanKind.INTERNAL,
                started_at=started_at,
                ended_at=self._clock.now(),
                status=status,
                attributes=dict(attributes),
                correlation=self._correlation(run_id),
            )
        )

    def emit_context_accounting(self, run_id: RunId, builder: object) -> None:
        """Emit context size/compaction metrics from a builder's accounting ledger."""
        if not isinstance(builder, ContextAccountingSource):
            return
        accounting = builder.last_accounting
        if accounting is None:
            return
        now = self._clock.now()
        correlation = self._correlation(run_id)
        samples = (
            (MetricName.CONTEXT_TOKENS_USED, float(accounting.used_tokens)),
            (MetricName.CONTEXT_TOKENS_USABLE, float(accounting.usable_tokens)),
            (MetricName.CONTEXT_ITEMS_KEPT, float(len(accounting.kept_entries))),
            (MetricName.CONTEXT_ITEMS_DROPPED, float(len(accounting.dropped_entries))),
            (
                MetricName.CONTEXT_ITEMS_COMPACTED,
                float(sum(1 for entry in accounting.kept_entries if entry.compacted)),
            ),
        )
        for name, value in samples:
            kind = MetricKind.GAUGE if name in _CONTEXT_GAUGES else MetricKind.COUNTER
            self._sink.emit(
                MetricSample(
                    name=name.value,
                    value=value,
                    occurred_at=now,
                    correlation=correlation,
                    kind=kind,
                    unit="token" if name in _CONTEXT_GAUGES else "1",
                )
            )

    def project_event(self, event: Event) -> None:  # noqa: PLR0912, PLR0915 - flat dispatch keeps each event projection a single auditable case
        """Project one authoritative domain event into logs and metrics.

        The event store remains the only source of truth; these records are a
        derived, non-authoritative narrative of what was already persisted.
        """
        state = self._state_for(event.run_id)
        match event:
            case RunStarted(objective=objective):
                state.started_at = event.occurred_at
                self._log(
                    event,
                    LogSeverity.INFO,
                    "run started",
                    attributes={"loopforge.objective": SensitiveText(objective)},
                )
                self._metric(event, MetricName.RUNS_STARTED, 1.0)
            case PlanCreated(plan=plan):
                self._log(
                    event,
                    LogSeverity.DEBUG,
                    "plan created",
                    attributes={"loopforge.plan": SensitiveText(plan)},
                )
            case ActionProposed(proposal=proposal):
                state.action_tools[str(proposal.action_id)] = proposal.tool_name
                self._log(
                    event,
                    LogSeverity.INFO,
                    "action proposed",
                    action_id=proposal.action_id,
                    tool_name=proposal.tool_name,
                )
            case ActionAuthorized(proposal=proposal, tool_metadata=metadata):
                state.action_tools[str(proposal.action_id)] = metadata.name
                state.action_sensitivities[str(proposal.action_id)] = metadata.sensitivity
                self._log(
                    event,
                    LogSeverity.INFO,
                    "action authorized",
                    action_id=proposal.action_id,
                    tool_name=metadata.name,
                    attributes={
                        "loopforge.tool.risk": metadata.risk.value,
                        "loopforge.tool.permission": metadata.required_permission.value,
                        "loopforge.tool.side_effect": metadata.side_effect.value,
                    },
                )
            case ActionRejected(proposal=proposal, reason_code=reason_code):
                self._log(
                    event,
                    LogSeverity.WARN,
                    "action rejected",
                    action_id=proposal.action_id,
                    tool_name=proposal.tool_name,
                    attributes={"loopforge.reject.reason_code": reason_code},
                )
            case ToolExecutionStarted(action_id=action_id, attempt=attempt):
                self._log(
                    event,
                    LogSeverity.DEBUG,
                    "tool execution started",
                    action_id=action_id,
                    tool_name=state.action_tools.get(str(action_id)),
                    attempt=attempt,
                )
            case ToolSucceeded(action_id=action_id, observation=observation, attempt=attempt):
                self._log(
                    event,
                    LogSeverity.INFO,
                    "tool succeeded",
                    action_id=action_id,
                    tool_name=state.action_tools.get(str(action_id)),
                    attempt=attempt,
                    attributes={
                        "loopforge.tool.observation": SensitiveText(
                            observation, _sensitivity_for(state, action_id)
                        )
                    },
                )
            case ToolFailed(
                action_id=action_id,
                error_code=error_code,
                error_message=error_message,
                failure_class=failure_class,
                attempt=attempt,
            ):
                self._log(
                    event,
                    LogSeverity.ERROR,
                    "tool failed",
                    action_id=action_id,
                    tool_name=state.action_tools.get(str(action_id)),
                    attempt=attempt,
                    attributes={
                        "loopforge.tool.error_code": error_code,
                        "loopforge.tool.error_message": SensitiveText(
                            error_message, _sensitivity_for(state, action_id)
                        ),
                        "loopforge.tool.failure_class": failure_class.value,
                    },
                )
                self._metric(
                    event,
                    MetricName.TOOL_FAILURES,
                    1.0,
                    attributes={"loopforge.tool.failure_class": failure_class.value},
                )
            case RetryScheduled(
                action_id=action_id,
                next_attempt=next_attempt,
                delay_seconds=delay_seconds,
                reason_code=reason_code,
            ):
                self._log(
                    event,
                    LogSeverity.WARN,
                    "retry scheduled",
                    action_id=action_id,
                    tool_name=state.action_tools.get(str(action_id)),
                    attributes={
                        "loopforge.retry.next_attempt": next_attempt,
                        "loopforge.retry.delay_seconds": delay_seconds,
                        "loopforge.retry.reason_code": reason_code,
                    },
                )
                self._metric(
                    event,
                    MetricName.RETRIES,
                    1.0,
                    attributes={"loopforge.retry.reason_code": reason_code},
                )
            case CircuitOpened(tool_name=tool_name, reason_code=reason_code):
                self._log(
                    event,
                    LogSeverity.WARN,
                    "circuit opened",
                    tool_name=tool_name,
                    attributes={"loopforge.circuit.reason_code": reason_code},
                )
                self._metric(event, MetricName.CIRCUITS_OPENED, 1.0, tool_name=tool_name)
            case VerificationPassed(summary=summary):
                self._log(
                    event,
                    LogSeverity.INFO,
                    "verification passed",
                    verification_id=_verification_id_for(event),
                    attributes={"loopforge.verification.summary": SensitiveText(summary)},
                )
            case VerificationFailed(summary=summary, score=score):
                attributes: dict[str, TelemetryAttributeValue | SensitiveText] = {
                    "loopforge.verification.summary": SensitiveText(summary)
                }
                if score is not None:
                    attributes["loopforge.verification.score"] = score
                self._log(
                    event,
                    LogSeverity.WARN,
                    "verification failed",
                    verification_id=_verification_id_for(event),
                    attributes=attributes,
                )
                self._metric(
                    event,
                    MetricName.VERIFICATION_FAILURES,
                    1.0,
                    verification_id=_verification_id_for(event),
                )
            case ReflectionRecorded(reflection=reflection):
                self._log(
                    event,
                    LogSeverity.INFO,
                    "reflection recorded",
                    attributes={"loopforge.reflection": SensitiveText(reflection)},
                )
            case ContextAssembled(
                context_items=items,
                prompt_template_id=template_id,
                prompt_template_version=template_version,
            ):
                context_attributes: dict[str, TelemetryAttributeValue | SensitiveText] = {
                    "loopforge.context.item_count": len(items)
                }
                if template_id is not None and template_version is not None:
                    context_attributes["loopforge.prompt.template_id"] = template_id
                    context_attributes["loopforge.prompt.template_version"] = template_version
                self._log(
                    event,
                    LogSeverity.DEBUG,
                    "context assembled",
                    attributes=context_attributes,
                )
                self._metric(event, MetricName.CYCLES, 1.0)
            case ArtifactRecorded(kind=kind, label=label, content=content):
                # The artifact payload itself is workload evidence and is never
                # copied into telemetry; only its code-owned shape is projected.
                self._log(
                    event,
                    LogSeverity.INFO,
                    "artifact recorded",
                    attributes={
                        "loopforge.artifact.kind": kind.value,
                        "loopforge.artifact.label": SensitiveText(label),
                        "loopforge.artifact.content_bytes": len(content.encode("utf-8")),
                    },
                )
            case BudgetDebited(usage=usage):
                self._log(
                    event,
                    LogSeverity.DEBUG,
                    "budget debited",
                    attributes={
                        "loopforge.usage.cost_usd": usage.cost_usd,
                        "loopforge.usage.input_tokens": usage.input_tokens,
                        "loopforge.usage.output_tokens": usage.output_tokens,
                        "loopforge.usage.cached_input_tokens": usage.cached_input_tokens,
                    },
                )
                now = event.occurred_at
                correlation = self._correlation(event.run_id)
                for name, value, unit in (
                    (MetricName.TOKENS_INPUT, float(usage.input_tokens), "token"),
                    (MetricName.TOKENS_OUTPUT, float(usage.output_tokens), "token"),
                    (
                        MetricName.TOKENS_CACHED_INPUT,
                        float(usage.cached_input_tokens),
                        "token",
                    ),
                    (MetricName.COST_USD, usage.cost_usd, "USD"),
                ):
                    self._sink.emit(
                        MetricSample(
                            name=name.value,
                            value=value,
                            occurred_at=now,
                            correlation=correlation,
                            unit=unit,
                        )
                    )
            case ApprovalRequested(action_id=action_id, reason=reason):
                self._log(
                    event,
                    LogSeverity.WARN,
                    "approval requested",
                    action_id=action_id,
                    attributes={"loopforge.approval.reason": SensitiveText(reason)},
                )
                self._metric(event, MetricName.APPROVALS_REQUESTED, 1.0, action_id=action_id)
            case ApprovalGranted(action_id=action_id):
                self._log(event, LogSeverity.INFO, "approval granted", action_id=action_id)
                self._metric(event, MetricName.APPROVALS_GRANTED, 1.0, action_id=action_id)
            case RunStopped(reason=reason, summary=summary):
                severity = (
                    LogSeverity.INFO
                    if reason in {StopReason.SUCCESS_VERIFIED, StopReason.CANCELLED}
                    else LogSeverity.WARN
                )
                self._log(
                    event,
                    severity,
                    "run stopped",
                    attributes={
                        "loopforge.stop.reason": reason.value,
                        "loopforge.stop.summary": SensitiveText(summary),
                    },
                )
                self._metric(
                    event,
                    MetricName.RUNS_COMPLETED,
                    1.0,
                    attributes={"loopforge.run.outcome": reason.value},
                )
                if state.started_at is not None:
                    self._metric(
                        event,
                        MetricName.RUN_DURATION_SECONDS,
                        (event.occurred_at - state.started_at).total_seconds(),
                        kind=MetricKind.HISTOGRAM,
                        unit="s",
                    )
                if reason is StopReason.STALLED:
                    self._metric(event, MetricName.STALLS, 1.0)
                if reason is StopReason.BUDGET_EXHAUSTED:
                    self._metric(event, MetricName.BUDGET_STOPS, 1.0)

    def _log(  # noqa: PLR0913 - keyword-only correlation plumbing keeps every call site explicit
        self,
        event: Event,
        severity: LogSeverity,
        message: str,
        *,
        action_id: ActionId | None = None,
        tool_name: str | None = None,
        attempt: int | None = None,
        verification_id: VerificationId | None = None,
        attributes: Mapping[str, TelemetryAttributeValue | SensitiveText] | None = None,
    ) -> None:
        self._sink.emit(
            LogRecord(
                message=message,
                occurred_at=event.occurred_at,
                severity=severity,
                trace_id=self.trace_id_for(event.run_id),
                attributes=dict(attributes) if attributes is not None else {},
                correlation=self._correlation(
                    event.run_id,
                    action_id=action_id,
                    tool_name=tool_name,
                    attempt=attempt,
                    verification_id=verification_id,
                ),
            )
        )

    def _metric(  # noqa: PLR0913 - keyword-only correlation plumbing keeps every call site explicit
        self,
        event: Event,
        name: MetricName,
        value: float,
        *,
        kind: MetricKind = MetricKind.COUNTER,
        unit: str = "1",
        tool_name: str | None = None,
        action_id: ActionId | None = None,
        verification_id: VerificationId | None = None,
        attributes: Mapping[str, TelemetryAttributeValue | SensitiveText] | None = None,
    ) -> None:
        self._sink.emit(
            MetricSample(
                name=name.value,
                value=value,
                occurred_at=event.occurred_at,
                kind=kind,
                unit=unit,
                attributes=dict(attributes) if attributes is not None else {},
                correlation=self._correlation(
                    event.run_id,
                    action_id=action_id,
                    tool_name=tool_name,
                    verification_id=verification_id,
                ),
            )
        )


_CONTEXT_GAUGES = frozenset({MetricName.CONTEXT_TOKENS_USED, MetricName.CONTEXT_TOKENS_USABLE})


def _sensitivity_for(state: _TraceState, action_id: ActionId) -> DataSensitivity:
    # Fail closed: without a recorded authorization, treat outcome text as sensitive.
    return state.action_sensitivities.get(str(action_id), DataSensitivity.SENSITIVE)


def _verification_id_for(event: VerificationPassed | VerificationFailed) -> VerificationId:
    """Derive the verification correlation id from the authoritative event sequence."""
    return VerificationId(f"{event.run_id}:verification:{event.sequence}")
