"""Every side-effect dispatch, including recovery/retry, obeys current control."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import ObservationContainsVerifier, ScriptedModel, ScriptedTools
from loopforge.application.runtime import Runtime, UnsafeResumeStateError
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    RunStopped,
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
from loopforge.domain.types import (
    ActionId,
    BudgetLimit,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    RunStatus,
)
from loopforge.ports.tools import ToolExecutionRequest, ToolResult

NOW = datetime(2026, 10, 26, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


class Sleeper:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def sleep(self, seconds: float) -> None:
        self.clock.current += timedelta(seconds=seconds)


class Tools(ScriptedTools):
    def __init__(self, metadata: ToolMetadata, results: list[ToolResult]) -> None:
        super().__init__(results, metadata=[metadata])
        self.requests: list[ToolExecutionRequest] = []

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        self.requests.append(request)
        return super().execute(request)


def metadata(*, idempotent: bool = False) -> ToolMetadata:
    return ToolMetadata(
        name="edit",
        risk=RiskLevel.LOCAL_WRITE,
        required_permission=Permission.LOCAL_WRITE,
        side_effect=SideEffectClass.LOCAL_WRITE,
        retry=RetryClass.TRANSIENT_ONLY if idempotent else RetryClass.NEVER,
        idempotency=IdempotencyClass.NATURAL if idempotent else IdempotencyClass.NONE,
        approval=ApprovalClass.NONE,
        timeout_seconds=5,
    )


def runtime(clock: Clock, tools: Tools) -> Runtime:
    return Runtime(
        model=ScriptedModel([ActionProposal(ActionId("proposed"), "edit", {})]),
        tools=tools,
        verifier=ObservationContainsVerifier("done"),
        store=InMemoryEventStore(),
        control=ControlPolicy(
            BudgetLimit(max_cost_usd=1, max_iterations=1), first_local_write_seconds=480
        ),
        permissions=PermissionPolicy(frozenset({Permission.LOCAL_WRITE})),
        reliability=ReliabilityPolicy(
            retry=RetrySettings(base_delay_seconds=2, max_delay_seconds=2, jitter_fraction=0)
        ),
        context=BasicContextBuilder(clock),
        clock=clock,
        sleeper=Sleeper(clock),
    )


def authorize(runtime: Runtime, tool: ToolMetadata, *, in_flight: bool = False) -> RunId:
    run_id = runtime.start("Make one edit")
    proposal = ActionProposal(ActionId("authorized"), "edit", {})
    events = [
        ActionProposed(
            event_id=EventId("p"), run_id=run_id, sequence=3, occurred_at=NOW, proposal=proposal
        ),
        ActionAuthorized(
            event_id=EventId("a"),
            run_id=run_id,
            sequence=4,
            occurred_at=NOW,
            proposal=proposal,
            tool_metadata=tool,
        ),
    ]
    for event in events:
        runtime.store.append(event, expected_version=event.sequence - 1)
    if in_flight:
        event = ToolExecutionStarted(
            event_id=EventId("x"),
            run_id=run_id,
            sequence=5,
            occurred_at=NOW,
            action_id=proposal.action_id,
            attempt=1,
            idempotency_key=None,
        )
        runtime.store.append(event, expected_version=4)
    return run_id


def test_last_admitted_iteration_may_execute() -> None:
    clock = Clock()
    tools = Tools(metadata(), [ToolResult(ok=True, observation="done")])
    app = runtime(clock, tools)
    final = app.run("One edit is permitted")
    assert final.status is RunStatus.SUCCEEDED
    assert final.iteration == 1
    assert len(tools.requests) == 1


@pytest.mark.parametrize("wall_deadline", [False, True])
@pytest.mark.parametrize("in_flight", [False, True])
def test_recovery_does_not_dispatch_after_a_deadline(wall_deadline: bool, in_flight: bool) -> None:
    clock = Clock()
    tool = metadata(idempotent=True)
    tools = Tools(tool, [ToolResult(ok=True, observation="done")])
    app = runtime(clock, tools)
    if wall_deadline:
        app.control = replace(
            app.control,
            first_local_write_seconds=None,
            budget=replace(app.control.budget, max_elapsed_seconds=480),
        )
    run_id = authorize(app, tool, in_flight=in_flight)
    clock.current += timedelta(seconds=481)
    final = app.resume(run_id)
    assert (
        final.status is RunStatus.FAILED
        if not wall_deadline
        else final.status is RunStatus.BUDGET_EXHAUSTED
    )
    assert not tools.requests
    events = app.store.events_for(run_id)
    assert isinstance(events[-1], RunStopped)
    assert events[-1].reason_code == (
        "STOP_TIME_BUDGET_EXHAUSTED" if wall_deadline else "STOP_FIRST_WRITE_DEADLINE"
    )


def test_retry_wait_cannot_dispatch_after_first_write_deadline() -> None:
    clock = Clock()
    tool = metadata(idempotent=True)

    class FirstAttempt(Tools):
        def execute(self, request: ToolExecutionRequest) -> ToolResult:
            clock.current = NOW + timedelta(seconds=479)
            return super().execute(request)

    tools = FirstAttempt(
        tool,
        [
            ToolResult(
                ok=False,
                observation="try again",
                error_code="TRANSIENT",
                failure_class=ToolFailureClass.TRANSIENT,
            ),
            ToolResult(ok=True, observation="done"),
        ],
    )
    app = runtime(clock, tools)
    final = app.run("Retry is bounded by the deadline")
    assert final.status is RunStatus.FAILED
    assert len(tools.requests) == 1
    assert final.first_local_write_at is None
    assert app.resume(final.run_id) == final


def test_non_idempotent_in_flight_outcome_remains_ambiguous_without_reexecution() -> None:
    clock = Clock()
    tool = metadata()
    tools = Tools(tool, [ToolResult(ok=True, observation="done")])
    app = runtime(clock, tools)
    run_id = authorize(app, tool, in_flight=True)
    clock.current += timedelta(seconds=481)
    with pytest.raises(UnsafeResumeStateError, match="ambiguous"):
        app.resume(run_id)
    assert not tools.requests


def test_admitted_action_cannot_exceed_iteration_cap() -> None:
    clock = Clock()
    tools = Tools(metadata(), [])
    app = runtime(clock, tools)
    run_id = authorize(app, metadata())
    state = app.state_for(run_id)
    assert app.control.evaluate(state, now=NOW, action_admitted=True).stop_reason is None
    over = replace(state, iteration=state.iteration + 1)
    assert (
        app.control.evaluate(over, now=NOW, action_admitted=True).reason_code
        == "STOP_MAX_ITERATIONS"
    )
