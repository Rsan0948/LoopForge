from datetime import UTC, datetime
from pathlib import Path

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
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    ActionRejected,
    CircuitOpened,
    RetryScheduled,
    ToolExecutionStarted,
)
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy, RetrySettings, ToolFailureClass
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, BudgetLimit, EventId, Permission, RiskLevel, RunStatus
from loopforge.ports.tools import ToolExecutionRequest, ToolResult

NOW = datetime(2026, 8, 22, 19, 0, tzinfo=UTC)


def _external_keyed(name: str = "remote_write") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.EXTERNAL_WRITE,
        required_permission=Permission.EXTERNAL_WRITE,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        retry=RetryClass.TRANSIENT_ONLY,
        idempotency=IdempotencyClass.KEYED,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _read(name: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


class AmbiguousIdempotentTool:
    def __init__(self, *, already_executed: set[str] | None = None) -> None:
        self.metadata = _external_keyed()
        self.executed = set(already_executed or set())
        self.side_effect_count = len(self.executed)
        self.requests: list[ToolExecutionRequest] = []

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        if tool_name != self.metadata.name:
            raise LookupError(tool_name)
        return self.metadata

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        self.requests.append(request)
        key = request.idempotency_key
        assert key is not None
        if key in self.executed:
            return ToolResult(ok=True, observation="remote write complete")
        self.executed.add(key)
        self.side_effect_count += 1
        return ToolResult(
            ok=False,
            observation="remote accepted request but response was lost",
            error_code="TIMEOUT_AFTER_SEND",
            failure_class=ToolFailureClass.AMBIGUOUS_OUTCOME,
        )


def test_ambiguous_remote_success_retries_with_same_key_without_duplicate_side_effect() -> None:
    store = InMemoryEventStore()
    tool = AmbiguousIdempotentTool()
    sleeper = RecordingSleeper()
    runtime = Runtime(
        model=ScriptedModel([ActionProposal(ActionId("write-1"), "remote_write", {})]),
        tools=tool,
        verifier=ObservationContainsVerifier("remote write complete"),
        store=store,
        control=ControlPolicy(BudgetLimit(5.0, 10)),
        permissions=PermissionPolicy(frozenset({Permission.EXTERNAL_WRITE})),
        reliability=ReliabilityPolicy(
            retry=RetrySettings(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0)
        ),
        clock=FixedClock(NOW),
        sleeper=sleeper,
    )

    state = runtime.run("perform one idempotent remote write")

    assert state.status is RunStatus.SUCCEEDED
    assert tool.side_effect_count == 1
    assert len(tool.requests) == 2
    assert tool.requests[0].idempotency_key == tool.requests[1].idempotency_key
    assert any(isinstance(event, RetryScheduled) for event in store.events_for(state.run_id))


def test_restart_from_ambiguous_keyed_execution_replays_same_attempt_safely(tmp_path: Path) -> None:
    path = tmp_path / "reliability.db"
    store = SQLiteEventStore(path, codec=JsonEventCodec())
    proposal = ActionProposal(ActionId("write-1"), "remote_write", {})
    clock = FixedClock(NOW)
    starter_tool = AmbiguousIdempotentTool()
    runtime = Runtime(
        model=ScriptedModel([]),
        tools=starter_tool,
        verifier=ObservationContainsVerifier("remote write complete"),
        store=store,
        control=ControlPolicy(BudgetLimit(5.0, 10)),
        permissions=PermissionPolicy(frozenset({Permission.EXTERNAL_WRITE})),
        reliability=ReliabilityPolicy(),
        clock=clock,
        sleeper=RecordingSleeper(),
    )
    run_id = runtime.start("resume ambiguous write")

    def append(event) -> None:
        version = store.current_version(run_id)
        store.append(event(version + 1), expected_version=version)

    append(
        lambda sequence: ActionProposed(
            event_id=EventId("p"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            proposal=proposal,
        )
    )
    append(
        lambda sequence: ActionAuthorized(
            event_id=EventId("a"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            proposal=proposal,
            tool_metadata=_external_keyed(),
        )
    )
    key = f"loopforge:{run_id}:{proposal.action_id}"
    append(
        lambda sequence: ToolExecutionStarted(
            event_id=EventId("s"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            action_id=proposal.action_id,
            attempt=1,
            idempotency_key=key,
        )
    )

    recovered_tool = AmbiguousIdempotentTool(already_executed={key})
    recovered = Runtime(
        model=ScriptedModel([]),
        tools=recovered_tool,
        verifier=ObservationContainsVerifier("remote write complete"),
        store=SQLiteEventStore(path, codec=JsonEventCodec()),
        control=ControlPolicy(BudgetLimit(5.0, 10)),
        permissions=PermissionPolicy(frozenset({Permission.EXTERNAL_WRITE})),
        reliability=ReliabilityPolicy(),
        clock=clock,
        sleeper=RecordingSleeper(),
    ).resume(run_id)

    assert recovered.status is RunStatus.SUCCEEDED
    assert recovered_tool.side_effect_count == 1
    assert recovered_tool.requests[0].idempotency_key == key
    starts = [e for e in store.events_for(run_id) if isinstance(e, ToolExecutionStarted)]
    assert len(starts) == 1


def test_circuit_opens_after_consecutive_failures_and_blocks_same_tool() -> None:
    store = InMemoryEventStore()
    tools = ScriptedTools(
        [
            ToolResult(
                ok=False,
                observation="dependency down",
                error_code="503",
                failure_class=ToolFailureClass.TRANSIENT,
            ),
            ToolResult(
                ok=False,
                observation="dependency down",
                error_code="503",
                failure_class=ToolFailureClass.TRANSIENT,
            ),
            ToolResult(ok=True, observation="all tests pass"),
        ],
        metadata=[_read("dependency"), _read("fallback")],
    )
    runtime = Runtime(
        model=ScriptedModel(
            [
                ActionProposal(ActionId("a1"), "dependency", {}),
                ActionProposal(ActionId("a2"), "dependency", {}),
                ActionProposal(ActionId("a3"), "dependency", {}),
                ActionProposal(ActionId("a4"), "fallback", {}),
            ]
        ),
        tools=tools,
        verifier=ObservationContainsVerifier("all tests pass"),
        store=store,
        control=ControlPolicy(BudgetLimit(5.0, 10), no_progress_limit=5),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(
            retry=RetrySettings(max_attempts=1), circuit_failure_threshold=2
        ),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
    )

    state = runtime.run("recover from dependency outage")
    events = store.events_for(state.run_id)
    assert state.status is RunStatus.SUCCEEDED
    assert any(isinstance(event, CircuitOpened) for event in events)
    assert any(
        isinstance(event, ActionRejected) and event.reason_code == "BLOCK_CIRCUIT_OPEN"
        for event in events
    )


def test_repeated_non_improving_verification_stops_as_stalled() -> None:
    store = InMemoryEventStore()
    runtime = Runtime(
        model=ScriptedModel(
            [
                ActionProposal(ActionId("a1"), "one", {}),
                ActionProposal(ActionId("a2"), "two", {}),
                ActionProposal(ActionId("a3"), "three", {}),
            ]
        ),
        tools=ScriptedTools(
            [
                ToolResult(ok=True, observation="still broken"),
                ToolResult(ok=True, observation="still broken"),
                ToolResult(ok=True, observation="still broken"),
            ],
            metadata=[_read("one"), _read("two"), _read("three")],
        ),
        verifier=ObservationContainsVerifier("never appears"),
        store=store,
        control=ControlPolicy(BudgetLimit(5.0, 10), no_progress_limit=2),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(circuit_failure_threshold=10),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
    )

    state = runtime.run("detect stall")
    assert state.status is RunStatus.STALLED
    assert state.consecutive_no_progress == 2


def test_resume_honors_remaining_persisted_retry_backoff(tmp_path: Path) -> None:
    path = tmp_path / "retry-resume.db"
    store = SQLiteEventStore(path, codec=JsonEventCodec())
    proposal = ActionProposal(ActionId("retry-1"), "dependency", {})
    metadata = ToolMetadata(
        name="dependency",
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=2.0,
    )
    runtime = Runtime(
        model=ScriptedModel([]),
        tools=ScriptedTools([], metadata=[metadata]),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=store,
        control=ControlPolicy(BudgetLimit(5.0, 10)),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
    )
    run_id = runtime.start("resume retry")

    def append(event) -> None:
        version = store.current_version(run_id)
        store.append(event(version + 1), expected_version=version)

    append(
        lambda sequence: ActionProposed(
            event_id=EventId("rp"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            proposal=proposal,
        )
    )
    append(
        lambda sequence: ActionAuthorized(
            event_id=EventId("ra"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            proposal=proposal,
            tool_metadata=metadata,
        )
    )
    append(
        lambda sequence: ToolExecutionStarted(
            event_id=EventId("rs"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            action_id=proposal.action_id,
            attempt=1,
            idempotency_key=None,
        )
    )
    from loopforge.domain.events import ToolFailed

    append(
        lambda sequence: ToolFailed(
            event_id=EventId("rf"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            action_id=proposal.action_id,
            error_code="503",
            error_message="temporary outage",
            failure_class=ToolFailureClass.TRANSIENT,
            attempt=1,
        )
    )
    append(
        lambda sequence: RetryScheduled(
            event_id=EventId("rr"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            action_id=proposal.action_id,
            next_attempt=2,
            delay_seconds=5.0,
            reason_code="RETRY_TRANSIENT_FAILURE",
        )
    )

    sleeper = RecordingSleeper()
    resumed = Runtime(
        model=ScriptedModel([]),
        tools=ScriptedTools(
            [ToolResult(ok=True, observation="all tests pass")], metadata=[metadata]
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=SQLiteEventStore(path, codec=JsonEventCodec()),
        control=ControlPolicy(BudgetLimit(5.0, 10)),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(),
        clock=FixedClock(NOW),
        sleeper=sleeper,
    ).resume(run_id)

    assert resumed.status is RunStatus.SUCCEEDED
    assert sleeper.delays == [5.0]
