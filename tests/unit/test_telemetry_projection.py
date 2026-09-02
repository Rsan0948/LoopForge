from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict, get_args

from loopforge.adapters.scripted import FixedClock
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.telemetry import RuntimeTelemetry
from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import ArtifactKind
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
from loopforge.domain.telemetry import (
    REDACTION_PLACEHOLDER,
    LogSeverity,
    MetricKind,
    MetricName,
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
    EventId,
    Permission,
    RiskLevel,
    RunId,
    StopReason,
    UsageDelta,
    VerificationId,
    WorkerId,
    WorkspaceId,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
RUN_ID = RunId("run_projection1")
ACTION_ID = ActionId("a1")


def _telemetry() -> tuple[RuntimeTelemetry, InMemoryTelemetry]:
    sink = InMemoryTelemetry()
    return RuntimeTelemetry(sink, FixedClock(NOW)), sink


def _metadata(*, sensitivity: DataSensitivity = DataSensitivity.INTERNAL) -> ToolMetadata:
    return ToolMetadata(
        name="inspect",
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
        sensitivity=sensitivity,
    )


class _EventFields(TypedDict):
    event_id: EventId
    run_id: RunId
    occurred_at: datetime
    sequence: int


def _event_fields(sequence: int, occurred_at: datetime = NOW) -> _EventFields:
    return {
        "event_id": EventId(f"evt_{sequence:016d}"),
        "run_id": RUN_ID,
        "occurred_at": occurred_at,
        "sequence": sequence,
    }


def test_run_started_projects_log_and_counter() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(RunStarted(**_event_fields(1), objective="fix the bug"))
    log = sink.logs[0]
    assert log.message == "run started"
    assert log.severity is LogSeverity.INFO
    assert log.attributes["loopforge.objective"] == "fix the bug"
    assert log.trace_id == str(RUN_ID)
    assert sink.metrics[0].name == MetricName.RUNS_STARTED.value
    assert sink.metrics[0].value == 1.0


def test_plan_created_projects_debug_log() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(PlanCreated(**_event_fields(2), plan="do things"))
    assert sink.logs[0].severity is LogSeverity.DEBUG
    assert sink.logs[0].attributes["loopforge.plan"] == "do things"
    assert sink.metrics == ()


def test_action_proposed_learns_tool_correlation() -> None:
    telemetry, sink = _telemetry()
    proposal = ActionProposal(ACTION_ID, "inspect", {})
    telemetry.project_event(ActionProposed(**_event_fields(3), proposal=proposal))
    log = sink.logs[0]
    assert log.correlation.action_id == ACTION_ID
    assert log.correlation.tool_name == "inspect"


def test_action_authorized_records_metadata_and_sensitivity() -> None:
    telemetry, sink = _telemetry()
    proposal = ActionProposal(ACTION_ID, "inspect", {})
    telemetry.project_event(
        ActionAuthorized(**_event_fields(4), proposal=proposal, tool_metadata=_metadata())
    )
    log = sink.logs[0]
    assert log.attributes["loopforge.tool.risk"] == "read_only"
    assert log.attributes["loopforge.tool.permission"] == "read"
    assert log.attributes["loopforge.tool.side_effect"] == "read_only"


def test_action_rejected_projects_warning_with_reason_code() -> None:
    telemetry, sink = _telemetry()
    proposal = ActionProposal(ACTION_ID, "inspect", {})
    telemetry.project_event(
        ActionRejected(**_event_fields(4), proposal=proposal, reason_code="BLOCK_PERMISSION_DENIED")
    )
    log = sink.logs[0]
    assert log.severity is LogSeverity.WARN
    assert log.attributes["loopforge.reject.reason_code"] == "BLOCK_PERMISSION_DENIED"


def test_tool_events_inherit_tool_correlation_from_proposal() -> None:
    telemetry, sink = _telemetry()
    proposal = ActionProposal(ACTION_ID, "inspect", {})
    telemetry.project_event(ActionProposed(**_event_fields(3), proposal=proposal))
    telemetry.project_event(
        ToolExecutionStarted(
            **_event_fields(5), action_id=ACTION_ID, attempt=1, idempotency_key=None
        )
    )
    started = sink.logs[-1]
    assert started.correlation.tool_name == "inspect"
    assert started.correlation.attempt == 1


def test_tool_succeeded_observation_passes_through_for_internal_tools() -> None:
    telemetry, sink = _telemetry()
    proposal = ActionProposal(ACTION_ID, "inspect", {})
    telemetry.project_event(
        ActionAuthorized(**_event_fields(4), proposal=proposal, tool_metadata=_metadata())
    )
    telemetry.project_event(
        ToolSucceeded(**_event_fields(6), action_id=ACTION_ID, observation="raw output", attempt=1)
    )
    assert sink.logs[-1].attributes["loopforge.tool.observation"] == "raw output"


def test_tool_succeeded_observation_is_redacted_for_sensitive_tools() -> None:
    telemetry, sink = _telemetry()
    proposal = ActionProposal(ACTION_ID, "inspect", {})
    telemetry.project_event(
        ActionAuthorized(
            **_event_fields(4),
            proposal=proposal,
            tool_metadata=_metadata(sensitivity=DataSensitivity.SENSITIVE),
        )
    )
    telemetry.project_event(
        ToolSucceeded(**_event_fields(6), action_id=ACTION_ID, observation="secret-ish", attempt=1)
    )
    assert sink.logs[-1].attributes["loopforge.tool.observation"] == REDACTION_PLACEHOLDER


def test_tool_outcome_redaction_fails_closed_without_authorization() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        ToolSucceeded(
            **_event_fields(6), action_id=ACTION_ID, observation="unknown-origin", attempt=1
        )
    )
    assert sink.logs[-1].attributes["loopforge.tool.observation"] == REDACTION_PLACEHOLDER


def test_tool_failed_projects_error_log_and_failure_metric() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        ToolFailed(
            **_event_fields(6),
            action_id=ACTION_ID,
            error_code="TIMEOUT",
            error_message="timed out",
            failure_class=ToolFailureClass.TRANSIENT,
            attempt=1,
        )
    )
    log = sink.logs[0]
    assert log.severity is LogSeverity.ERROR
    assert log.attributes["loopforge.tool.error_code"] == "TIMEOUT"
    assert log.attributes["loopforge.tool.failure_class"] == "transient"
    metric = sink.metrics[0]
    assert metric.name == MetricName.TOOL_FAILURES.value
    assert metric.attributes["loopforge.tool.failure_class"] == "transient"


def test_retry_scheduled_projects_log_and_retry_metric() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        RetryScheduled(
            **_event_fields(7),
            action_id=ACTION_ID,
            next_attempt=2,
            delay_seconds=0.5,
            reason_code="RETRY_TRANSIENT_FAILURE",
        )
    )
    log = sink.logs[0]
    assert log.severity is LogSeverity.WARN
    assert log.attributes["loopforge.retry.next_attempt"] == 2
    assert log.attributes["loopforge.retry.delay_seconds"] == 0.5
    metric = sink.metrics[0]
    assert metric.name == MetricName.RETRIES.value
    assert metric.attributes["loopforge.retry.reason_code"] == "RETRY_TRANSIENT_FAILURE"


def test_circuit_opened_projects_log_and_metric_with_tool_correlation() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        CircuitOpened(
            **_event_fields(8),
            tool_name="inspect",
            reason_code="CIRCUIT_OPEN_CONSECUTIVE_FAILURES",
        )
    )
    assert sink.logs[0].correlation.tool_name == "inspect"
    assert sink.metrics[0].name == MetricName.CIRCUITS_OPENED.value
    assert sink.metrics[0].correlation.tool_name == "inspect"


def test_verification_passed_derives_verification_id_from_event_sequence() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(VerificationPassed(**_event_fields(9), summary="looks good"))
    log = sink.logs[0]
    assert log.correlation.verification_id == VerificationId(f"{RUN_ID}:verification:9")
    assert log.attributes["loopforge.verification.summary"] == "looks good"


def test_verification_failed_projects_metric_and_optional_score() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        VerificationFailed(**_event_fields(9), summary="still broken", score=0.25)
    )
    log = sink.logs[0]
    assert log.severity is LogSeverity.WARN
    assert log.attributes["loopforge.verification.score"] == 0.25
    metric = sink.metrics[0]
    assert metric.name == MetricName.VERIFICATION_FAILURES.value
    assert metric.correlation.verification_id == VerificationId(f"{RUN_ID}:verification:9")


def test_verification_failed_omits_score_when_absent() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(VerificationFailed(**_event_fields(9), summary="still broken"))
    assert "loopforge.verification.score" not in sink.logs[0].attributes


def test_reflection_recorded_projects_log() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(ReflectionRecorded(**_event_fields(10), reflection="try harder"))
    assert sink.logs[0].attributes["loopforge.reflection"] == "try harder"


def test_artifact_recorded_projects_log_without_content_payload() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        ArtifactRecorded(
            **_event_fields(11),
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="workspace:fixture",
            content="exact patch bytes",
        )
    )
    log = sink.logs[0]
    assert log.message == "artifact recorded"
    assert log.severity is LogSeverity.INFO
    assert log.attributes["loopforge.artifact.kind"] == "workspace_snapshot"
    assert log.attributes["loopforge.artifact.label"] == "workspace:fixture"
    assert log.attributes["loopforge.artifact.content_bytes"] == len(b"exact patch bytes")
    # The evidence payload itself never enters the telemetry projection.
    assert "exact patch bytes" not in log.attributes.values()
    assert sink.metrics == ()


def test_context_assembled_projects_cycle_metric_and_template_metadata() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        ContextAssembled(
            **_event_fields(3),
            context_items=(),
            prompt_template_id="loopforge.controller",
            prompt_template_version="1.0.0",
        )
    )
    log = sink.logs[0]
    assert log.attributes["loopforge.context.item_count"] == 0
    assert log.attributes["loopforge.prompt.template_id"] == "loopforge.controller"
    assert sink.metrics[0].name == MetricName.CYCLES.value


def test_context_assembled_without_template_omits_template_attributes() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        ContextAssembled(
            **_event_fields(3),
            context_items=(),
            prompt_template_id=None,
            prompt_template_version=None,
        )
    )
    assert "loopforge.prompt.template_id" not in sink.logs[0].attributes


def test_budget_debited_projects_token_cost_and_cache_metrics() -> None:
    telemetry, sink = _telemetry()
    usage = UsageDelta(cost_usd=0.02, input_tokens=100, output_tokens=20, cached_input_tokens=40)
    telemetry.project_event(BudgetDebited(**_event_fields(4), usage=usage))
    metrics = {sample.name: sample for sample in sink.metrics}
    assert metrics[MetricName.TOKENS_INPUT.value].value == 100.0
    assert metrics[MetricName.TOKENS_INPUT.value].unit == "token"
    assert metrics[MetricName.TOKENS_OUTPUT.value].value == 20.0
    assert metrics[MetricName.TOKENS_CACHED_INPUT.value].value == 40.0
    assert metrics[MetricName.COST_USD.value].value == 0.02
    assert metrics[MetricName.COST_USD.value].unit == "USD"
    log = sink.logs[0]
    assert log.attributes["loopforge.usage.cached_input_tokens"] == 40


def test_approval_events_project_logs_and_metrics() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        ApprovalRequested(**_event_fields(5), action_id=ACTION_ID, reason="external write")
    )
    telemetry.project_event(ApprovalGranted(**_event_fields(6), action_id=ACTION_ID))
    requested, granted = sink.logs
    assert requested.severity is LogSeverity.WARN
    assert requested.attributes["loopforge.approval.reason"] == "external write"
    assert granted.severity is LogSeverity.INFO
    names = [sample.name for sample in sink.metrics]
    assert names == [
        MetricName.APPROVALS_REQUESTED.value,
        MetricName.APPROVALS_GRANTED.value,
    ]
    assert all(sample.correlation.action_id == ACTION_ID for sample in sink.metrics)


def test_run_stopped_projects_outcome_duration_and_stop_metrics() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(RunStarted(**_event_fields(1), objective="obj"))
    telemetry.project_event(
        RunStopped(
            **_event_fields(12, occurred_at=NOW + timedelta(seconds=30)),
            reason=StopReason.BUDGET_EXHAUSTED,
            summary="budget gone",
        )
    )
    metrics = {sample.name: sample for sample in sink.metrics}
    assert metrics[MetricName.RUNS_COMPLETED.value].attributes["loopforge.run.outcome"] == (
        "budget_exhausted"
    )
    duration = metrics[MetricName.RUN_DURATION_SECONDS.value]
    assert duration.value == 30.0
    assert duration.kind is MetricKind.HISTOGRAM
    assert duration.unit == "s"
    assert metrics[MetricName.BUDGET_STOPS.value].value == 1.0
    assert MetricName.STALLS.value not in metrics
    assert sink.logs[-1].severity is LogSeverity.WARN


def test_run_stopped_stalled_projects_stall_metric() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        RunStopped(**_event_fields(12), reason=StopReason.STALLED, summary="no progress")
    )
    names = {sample.name for sample in sink.metrics}
    assert MetricName.STALLS.value in names
    assert MetricName.BUDGET_STOPS.value not in names
    # Without a projected RunStarted there is no duration sample.
    assert MetricName.RUN_DURATION_SECONDS.value not in names


def test_run_stopped_success_and_cancel_log_at_info() -> None:
    telemetry, sink = _telemetry()
    telemetry.project_event(
        RunStopped(**_event_fields(12), reason=StopReason.SUCCESS_VERIFIED, summary="done")
    )
    telemetry.project_event(
        RunStopped(**_event_fields(13), reason=StopReason.CANCELLED, summary="operator")
    )
    assert [log.severity for log in sink.logs] == [LogSeverity.INFO, LogSeverity.INFO]


def test_projected_records_carry_current_cycle_correlation() -> None:
    telemetry, sink = _telemetry()
    telemetry.set_cycle(4)
    telemetry.project_event(PlanCreated(**_event_fields(2), plan="p"))
    assert sink.logs[0].correlation.cycle == 4


# --- PACS-010 hardening: projector exhaustiveness drift guard ---


def _catalog_examples() -> dict[type[Event], Event]:
    proposal = ActionProposal(ACTION_ID, "inspect", {})
    return {
        RunStarted: RunStarted(**_event_fields(1), objective="fix the bug"),
        PlanCreated: PlanCreated(**_event_fields(2), plan="do things"),
        ActionProposed: ActionProposed(**_event_fields(3), proposal=proposal),
        ActionAuthorized: ActionAuthorized(
            **_event_fields(4), proposal=proposal, tool_metadata=_metadata()
        ),
        ActionRejected: ActionRejected(
            **_event_fields(5), proposal=proposal, reason_code="BLOCKED"
        ),
        ToolExecutionStarted: ToolExecutionStarted(
            **_event_fields(6), action_id=ACTION_ID, attempt=1, idempotency_key=None
        ),
        ToolSucceeded: ToolSucceeded(
            **_event_fields(7), action_id=ACTION_ID, attempt=1, observation="ok"
        ),
        ToolFailed: ToolFailed(
            **_event_fields(8),
            action_id=ACTION_ID,
            attempt=1,
            error_code="X",
            error_message="boom",
            failure_class=ToolFailureClass.TRANSIENT,
        ),
        RetryScheduled: RetryScheduled(
            **_event_fields(9),
            action_id=ACTION_ID,
            next_attempt=2,
            delay_seconds=0.5,
            reason_code="RETRY",
        ),
        CircuitOpened: CircuitOpened(**_event_fields(10), tool_name="inspect", reason_code="X"),
        VerificationPassed: VerificationPassed(**_event_fields(11), summary="passed"),
        VerificationFailed: VerificationFailed(**_event_fields(12), summary="failed", score=0.5),
        ReflectionRecorded: ReflectionRecorded(**_event_fields(13), reflection="try again"),
        ContextAssembled: ContextAssembled(**_event_fields(14), context_items=()),
        ArtifactRecorded: ArtifactRecorded(
            **_event_fields(15),
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="workspace:fixture",
            content="evidence",
        ),
        BudgetDebited: BudgetDebited(
            **_event_fields(16),
            usage=UsageDelta(cost_usd=0.01, input_tokens=10, output_tokens=5),
        ),
        ApprovalRequested: ApprovalRequested(
            **_event_fields(17), action_id=ACTION_ID, reason="risky"
        ),
        ApprovalGranted: ApprovalGranted(**_event_fields(18), action_id=ACTION_ID),
        ApprovalRejected: ApprovalRejected(
            **_event_fields(22), action_id=ACTION_ID, reason="operator denied the write"
        ),
        OperatorInstruction: OperatorInstruction(
            **_event_fields(23),
            instruction="focus on the failing test only",
            amends_objective=True,
        ),
        RunStopped: RunStopped(
            **_event_fields(19), reason=StopReason.SUCCESS_VERIFIED, summary="done"
        ),
        WorkerSpawned: WorkerSpawned(
            **_event_fields(20),
            worker_id=WorkerId("worker-adder"),
            worker_run_id=RunId("worker-run-1"),
            workspace_id=WorkspaceId("ws-adder"),
            objective="repair adder.py",
            budget_share_cost_usd=0.5,
        ),
        WorkerStopped: WorkerStopped(
            **_event_fields(21),
            worker_id=WorkerId("worker-adder"),
            outcome=WorkerOutcome.SUCCEEDED,
            summary="worker finished",
        ),
        WorkerMerged: WorkerMerged(
            **_event_fields(22),
            worker_id=WorkerId("worker-adder"),
            outcome=MergeOutcome.MERGED,
            revision="a" * 40,
            detail="merged worker branch",
        ),
    }


def test_projector_maps_every_event_type_in_the_union() -> None:
    examples = _catalog_examples()
    # Drift guard: adding a 20th event type fails this test until a projector
    # arm (and its example here) exists — silent drops are not possible.
    assert set(examples) == set(get_args(Event))
    for event in examples.values():
        telemetry, sink = _telemetry()
        telemetry.project_event(event)
        assert sink.logs, f"{type(event).__name__} projected no structured log"
