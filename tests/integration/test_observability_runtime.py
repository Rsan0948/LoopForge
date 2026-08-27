from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from loopforge.adapters.context import (
    BasicContextBuilder,
    BudgetedContextBuilder,
    CharsPerTokenCounter,
)
from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
)
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy, RetrySettings, ToolFailureClass
from loopforge.domain.telemetry import (
    REDACTION_PLACEHOLDER,
    MetricName,
    SensitiveText,
    SpanName,
    SpanStatusCode,
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
    Permission,
    RiskLevel,
    RunId,
    RunStatus,
    StopReason,
    VerificationId,
)
from loopforge.entrypoints.cli import format_telemetry_narrative
from loopforge.ports.telemetry import TelemetryEmissionError, TelemetryPort
from loopforge.ports.tools import ToolResult

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _metadata(
    name: str,
    *,
    retry: RetryClass = RetryClass.SAFE,
    sensitivity: DataSensitivity = DataSensitivity.INTERNAL,
) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.LOCAL_WRITE,
        required_permission=Permission.LOCAL_WRITE,
        side_effect=SideEffectClass.LOCAL_WRITE,
        retry=retry,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
        sensitivity=sensitivity,
    )


def _runtime(  # noqa: PLR0913 - keyword-only scenario wiring keeps each test setup explicit
    *,
    telemetry: TelemetryPort | None,
    actions: list[ActionProposal] | None = None,
    results: list[ToolResult] | None = None,
    metadata: list[ToolMetadata] | None = None,
    expected_observation: str = "all tests pass",
    budget: BudgetLimit | None = None,
    reliability: ReliabilityPolicy | None = None,
    no_progress_limit: int = 3,
    budgeted_context: bool = False,
    store: object | None = None,
) -> Runtime:
    clock = FixedClock(NOW)
    if budgeted_context:
        context = BudgetedContextBuilder(
            clock,
            CharsPerTokenCounter(),
            template=default_controller_template(),
            token_budget=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
        )
    else:
        context = BasicContextBuilder(clock)
    return Runtime(
        model=ScriptedModel(
            actions
            if actions is not None
            else [ActionProposal(ActionId("a1"), "fix", {"target": "auth"})]
        ),
        tools=ScriptedTools(
            results if results is not None else [ToolResult(ok=True, observation="all tests pass")],
            metadata=metadata if metadata is not None else [_metadata("fix")],
        ),
        verifier=ObservationContainsVerifier(expected_observation),
        store=store if store is not None else InMemoryEventStore(),  # pyright: ignore[reportArgumentType]
        control=ControlPolicy(
            budget if budget is not None else BudgetLimit(max_cost_usd=5.0, max_iterations=10),
            no_progress_limit=no_progress_limit,
        ),
        permissions=PermissionPolicy(frozenset({Permission.LOCAL_WRITE})),
        reliability=reliability if reliability is not None else ReliabilityPolicy(),
        context=context,
        clock=clock,
        sleeper=RecordingSleeper(),
        telemetry=telemetry,
    )


def _metric_values(telemetry: InMemoryTelemetry) -> dict[str, list[float]]:
    values: dict[str, list[float]] = {}
    for sample in telemetry.metrics:
        values.setdefault(sample.name, []).append(sample.value)
    return values


# --- causally correlated trace/log narrative ------------------------------------


def test_successful_run_produces_a_causally_correlated_trace() -> None:
    telemetry = InMemoryTelemetry()
    state = _runtime(telemetry=telemetry).run("repair the regression")
    assert state.status is RunStatus.SUCCEEDED

    spans = telemetry.spans
    trace_ids = {span.trace_id for span in spans}
    assert trace_ids == {str(state.run_id)}

    span_ids = {span.span_id for span in spans}
    root_spans = [span for span in spans if span.name == SpanName.RUN.value]
    assert len(root_spans) == 1
    root = root_spans[0]
    assert root.parent_span_id is None
    assert root.status is SpanStatusCode.OK
    # Every non-root span parents into an emitted span, forming one tree.
    for span in spans:
        if span is root:
            continue
        assert span.parent_span_id in span_ids

    names = {span.name for span in spans}
    assert {
        SpanName.RUN.value,
        SpanName.CYCLE.value,
        SpanName.POLICY_DECISION.value,
        SpanName.CONTEXT_BUILD.value,
        SpanName.MODEL_TURN.value,
        SpanName.TOOL_EXECUTE.value,
        SpanName.VERIFY.value,
        SpanName.PERSIST.value,
    } <= names


def test_cycle_spans_parent_operation_spans() -> None:
    telemetry = InMemoryTelemetry()
    state = _runtime(telemetry=telemetry).run("repair the regression")
    root_id = f"{state.run_id}:span:0"
    spans = telemetry.spans
    cycles = [span for span in spans if span.name == SpanName.CYCLE.value]
    assert len(cycles) == 2  # one execution cycle + the terminal stop-check cycle
    assert all(span.parent_span_id == root_id for span in cycles)
    cycle_ids = {span.span_id for span in cycles}
    operations = [
        span for span in spans if span.name not in {SpanName.RUN.value, SpanName.CYCLE.value}
    ]
    assert operations
    # Operations parent into their cycle span; the two run-bootstrap persists
    # (RunStarted/PlanCreated) happen before the first cycle and parent to root.
    assert all(span.parent_span_id in cycle_ids | {root_id} for span in operations)


def test_verification_correlation_id_links_span_log_and_authoritative_sequence() -> None:
    telemetry = InMemoryTelemetry()
    store = InMemoryEventStore()
    state = _runtime(telemetry=telemetry, store=store).run("repair the regression")
    verify_spans = [span for span in telemetry.spans if span.name == SpanName.VERIFY.value]
    assert len(verify_spans) == 1
    verification_id = verify_spans[0].correlation.verification_id
    assert verification_id is not None

    verification_logs = [
        log for log in telemetry.logs if log.correlation.verification_id is not None
    ]
    assert len(verification_logs) == 1
    assert verification_logs[0].correlation.verification_id == verification_id

    # The correlation id points at the durable event sequence of the verification.
    events = store.events_for(state.run_id)
    verification_event = events[-2]  # VerificationPassed, then RunStopped
    assert verification_id == VerificationId(
        f"{state.run_id}:verification:{verification_event.sequence}"
    )


def test_metrics_cover_the_required_operational_surface() -> None:
    telemetry = InMemoryTelemetry()
    _runtime(telemetry=telemetry, budgeted_context=True).run("repair the regression")
    names = {sample.name for sample in telemetry.metrics}
    assert {
        MetricName.RUNS_STARTED.value,
        MetricName.RUNS_COMPLETED.value,
        MetricName.RUN_DURATION_SECONDS.value,
        MetricName.CYCLES.value,
        MetricName.TOKENS_INPUT.value,
        MetricName.TOKENS_OUTPUT.value,
        MetricName.TOKENS_CACHED_INPUT.value,
        MetricName.COST_USD.value,
        MetricName.CONTEXT_TOKENS_USED.value,
        MetricName.CONTEXT_TOKENS_USABLE.value,
        MetricName.CONTEXT_ITEMS_KEPT.value,
        MetricName.CONTEXT_ITEMS_DROPPED.value,
        MetricName.CONTEXT_ITEMS_COMPACTED.value,
    } <= names


def test_token_cost_and_cache_accounting_aggregates_model_usage() -> None:
    telemetry = InMemoryTelemetry()
    state = _runtime(telemetry=telemetry).run("repair the regression")
    values = _metric_values(telemetry)
    # ScriptedModel debits 100 input / 20 output / 0 cached / $0.01 per turn.
    assert values[MetricName.TOKENS_INPUT.value] == [100.0]
    assert values[MetricName.TOKENS_OUTPUT.value] == [20.0]
    assert values[MetricName.TOKENS_CACHED_INPUT.value] == [0.0]
    assert values[MetricName.COST_USD.value] == [0.01]
    assert state.input_tokens == 100


# --- redaction before export -----------------------------------------------------


def test_sensitive_tool_observation_is_redacted_in_telemetry_but_authoritative_in_store() -> None:
    telemetry = InMemoryTelemetry()
    store = InMemoryEventStore()
    state = _runtime(
        telemetry=telemetry,
        store=store,
        metadata=[_metadata("fix", sensitivity=DataSensitivity.SENSITIVE)],
    ).run("repair the regression")
    assert state.status is RunStatus.SUCCEEDED

    # The authoritative event store keeps the full observation.
    assert state.last_observation == "all tests pass"
    # Every telemetry record is redacted: no clear text, no SensitiveText wrappers.
    for record in telemetry.records:
        for value in record.attributes.values():
            assert not isinstance(value, SensitiveText)
            assert value != "all tests pass"
    observation_logs = [
        log for log in telemetry.logs if "loopforge.tool.observation" in log.attributes
    ]
    assert len(observation_logs) == 1
    assert observation_logs[0].attributes["loopforge.tool.observation"] == REDACTION_PLACEHOLDER


def test_internal_tool_observation_is_visible_in_telemetry() -> None:
    telemetry = InMemoryTelemetry()
    _runtime(telemetry=telemetry).run("repair the regression")
    observation_logs = [
        log for log in telemetry.logs if "loopforge.tool.observation" in log.attributes
    ]
    assert observation_logs[0].attributes["loopforge.tool.observation"] == "all tests pass"


# --- fail-safe observability -------------------------------------------------------


class _ThrowingTelemetry:
    def __init__(self) -> None:
        self.attempts = 0

    def emit(self, record: object) -> None:
        del record
        self.attempts += 1
        msg = "exporter unavailable"
        raise TelemetryEmissionError(msg)


def _serialized_events(runtime: Runtime, run_id: RunId) -> list[str]:
    codec = JsonEventCodec()
    normalized: list[str] = []
    for event in runtime.store.events_for(run_id):
        encoded = codec.encode(event)
        encoded = re.sub(r"run_[0-9a-f]{12}", "run_normalized", encoded)
        encoded = re.sub(r"evt_[0-9a-f]{16}", "evt_normalized", encoded)
        normalized.append(encoded)
    return normalized


def test_throwing_telemetry_adapter_cannot_corrupt_run_state() -> None:
    throwing = _ThrowingTelemetry()
    observed = _runtime(telemetry=throwing, store=InMemoryEventStore())
    state_with_failures = observed.run("repair the regression")

    baseline = _runtime(telemetry=None, store=InMemoryEventStore())
    state_without_telemetry = baseline.run("repair the regression")

    assert state_with_failures.status is RunStatus.SUCCEEDED
    # Every emission attempt failed, yet neither the outcome nor the durable
    # history changed: the authoritative stream is identical (modulo ids).
    assert throwing.attempts > 0
    assert _serialized_events(observed, state_with_failures.run_id) == _serialized_events(
        baseline, state_without_telemetry.run_id
    )


def test_runtime_without_telemetry_adapter_runs_clean() -> None:
    state = _runtime(telemetry=None).run("repair the regression")
    assert state.status is RunStatus.SUCCEEDED


# --- retry / circuit / budget / stall telemetry -----------------------------------


def test_retry_path_emits_retry_span_and_metric() -> None:
    telemetry = InMemoryTelemetry()
    sleeper_results = [
        ToolResult(
            ok=False,
            observation="flaky timeout",
            error_code="TIMEOUT",
            failure_class=ToolFailureClass.TRANSIENT,
        ),
        ToolResult(ok=True, observation="all tests pass"),
    ]
    state = _runtime(
        telemetry=telemetry,
        results=sleeper_results,
        reliability=ReliabilityPolicy(
            retry=RetrySettings(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0)
        ),
    ).run("repair the regression")
    assert state.status is RunStatus.SUCCEEDED

    retry_spans = [span for span in telemetry.spans if span.name == SpanName.RETRY.value]
    assert len(retry_spans) == 1
    assert retry_spans[0].attributes["loopforge.retry.reason_code"] == "RETRY_TRANSIENT_FAILURE"
    assert retry_spans[0].correlation.action_id == ActionId("a1")
    values = _metric_values(telemetry)
    assert values[MetricName.RETRIES.value] == [1.0]
    assert values[MetricName.TOOL_FAILURES.value] == [1.0]
    tool_spans = [span for span in telemetry.spans if span.name == SpanName.TOOL_EXECUTE.value]
    assert len(tool_spans) == 2
    assert [span.status for span in tool_spans] == [SpanStatusCode.ERROR, SpanStatusCode.OK]


def test_circuit_opening_emits_circuit_metric_and_tool_failure_counts() -> None:
    telemetry = InMemoryTelemetry()
    failure = ToolResult(
        ok=False,
        observation="broken",
        error_code="PERM",
        failure_class=ToolFailureClass.PERMANENT,
    )
    actions = [ActionProposal(ActionId(f"a{index}"), "fix", {}) for index in range(2)]
    state = _runtime(
        telemetry=telemetry,
        actions=actions,
        results=[failure, failure],
        expected_observation="never observed",
        reliability=ReliabilityPolicy(circuit_failure_threshold=2),
        budget=BudgetLimit(max_cost_usd=5.0, max_iterations=2),
    ).run("repair the regression")
    assert state.status is not RunStatus.SUCCEEDED

    values = _metric_values(telemetry)
    assert values[MetricName.CIRCUITS_OPENED.value] == [1.0]
    assert len(values[MetricName.TOOL_FAILURES.value]) == 2
    circuit_logs = [log for log in telemetry.logs if log.message == "circuit opened"]
    assert circuit_logs[0].correlation.tool_name == "fix"


def test_budget_exhaustion_emits_budget_stop_metric_and_error_run_span() -> None:
    telemetry = InMemoryTelemetry()
    state = _runtime(
        telemetry=telemetry,
        budget=BudgetLimit(max_cost_usd=0.005, max_iterations=10),
    ).run("repair the regression")
    assert state.status is RunStatus.BUDGET_EXHAUSTED

    values = _metric_values(telemetry)
    assert values[MetricName.BUDGET_STOPS.value] == [1.0]
    completed = [
        sample for sample in telemetry.metrics if sample.name == MetricName.RUNS_COMPLETED.value
    ]
    assert completed[0].attributes["loopforge.run.outcome"] == "budget_exhausted"
    root = next(span for span in telemetry.spans if span.name == SpanName.RUN.value)
    assert root.status is SpanStatusCode.ERROR
    assert root.attributes["loopforge.run.outcome"] == StopReason.BUDGET_EXHAUSTED.value


def test_stall_emits_stall_metric() -> None:
    telemetry = InMemoryTelemetry()
    failure_observation = ToolResult(ok=True, observation="still broken")
    actions = [ActionProposal(ActionId(f"a{index}"), "fix", {}) for index in range(3)]
    state = _runtime(
        telemetry=telemetry,
        actions=actions,
        results=[failure_observation] * 3,
        expected_observation="never observed",
        no_progress_limit=2,
    ).run("repair the regression")
    assert state.status is RunStatus.STALLED
    values = _metric_values(telemetry)
    assert values[MetricName.STALLS.value] == [1.0]
    verification_failures = values[MetricName.VERIFICATION_FAILURES.value]
    assert len(verification_failures) == 3


def test_rejected_action_emits_rejection_log_and_no_tool_span() -> None:
    telemetry = InMemoryTelemetry()
    state = _runtime(
        telemetry=telemetry,
        actions=[
            ActionProposal(ActionId("a1"), "ghost_tool", {}),
            ActionProposal(ActionId("a2"), "fix", {}),
        ],
    ).run("repair the regression")
    assert state.status is RunStatus.SUCCEEDED
    rejection_logs = [log for log in telemetry.logs if log.message == "action rejected"]
    assert rejection_logs[0].attributes["loopforge.reject.reason_code"] == "BLOCK_UNKNOWN_TOOL"
    tool_spans = [span for span in telemetry.spans if span.name == SpanName.TOOL_EXECUTE.value]
    assert len(tool_spans) == 1


# --- determinism and durable persistence ------------------------------------------


def _normalized_narrative(telemetry: InMemoryTelemetry, run_id: RunId) -> list[str]:
    narrative = format_telemetry_narrative(telemetry.records)
    return narrative.replace(str(run_id), "run_normalized").splitlines()


def test_equivalent_runs_produce_equivalent_telemetry() -> None:
    first_telemetry = InMemoryTelemetry()
    first = _runtime(telemetry=first_telemetry).run("repair the regression")
    second_telemetry = InMemoryTelemetry()
    second = _runtime(telemetry=second_telemetry).run("repair the regression")

    assert _normalized_narrative(first_telemetry, first.run_id) == _normalized_narrative(
        second_telemetry, second.run_id
    )


def test_telemetry_narrative_survives_sqlite_persistence(tmp_path: Path) -> None:
    telemetry = InMemoryTelemetry()
    store = SQLiteEventStore(tmp_path / "runs.db", codec=JsonEventCodec())
    state = _runtime(telemetry=telemetry, store=store).run("repair the regression")
    assert state.status is RunStatus.SUCCEEDED

    root = [span for span in telemetry.spans if span.name == SpanName.RUN.value]
    assert len(root) == 1
    assert root[0].status is SpanStatusCode.OK
    assert {span.trace_id for span in telemetry.spans} == {str(state.run_id)}
    # The authoritative stream is intact and replayable alongside telemetry.
    assert len(store.events_for(state.run_id)) == state.version
