"""Approval gateway and operator instructions through the runtime (PACS-014).

A stub model proposes approval-gated tools; the runtime must quiesce the run
on a durable ``ApprovalRequested`` and move only on durable operator
grant/reject events — including across a process restart (fresh runtime on
the same durable store). Approval can never expand authority: permissions are
authorized before the gateway is consulted, and again on the approved path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.context import BasicContextBuilder
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
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    OperatorInstruction,
    ToolExecutionStarted,
    ToolSucceeded,
)
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.state import InvalidTransitionError
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
    StopReason,
)
from loopforge.ports.state_store import StateStorePort
from loopforge.ports.tools import ToolResult

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _metadata(name: str, approval: ApprovalClass) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.LOCAL_WRITE,
        required_permission=Permission.LOCAL_WRITE,
        side_effect=SideEffectClass.LOCAL_WRITE,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NONE,
        approval=approval,
        timeout_seconds=5.0,
    )


def _proposal(action_id: str, tool_name: str = "deploy") -> ActionProposal:
    return ActionProposal(ActionId(action_id), tool_name, {"target": "workspace"})


def _runtime(
    model: ScriptedModel,
    *,
    store: StateStorePort,
    tools: ScriptedTools | None = None,
    permissions: frozenset[Permission] = frozenset({Permission.READ, Permission.LOCAL_WRITE}),
) -> Runtime:
    return Runtime(
        model=model,
        tools=tools
        or ScriptedTools(
            [ToolResult(ok=True, observation="all tests pass")],
            metadata=[_metadata("deploy", ApprovalClass.REQUIRED)],
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=store,
        control=ControlPolicy(BudgetLimit(max_cost_usd=1.0, max_iterations=5)),
        permissions=PermissionPolicy(permissions),
        reliability=ReliabilityPolicy(),
        context=BasicContextBuilder(FixedClock(NOW)),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
    )


def _model(*action_ids: str) -> ScriptedModel:
    return ScriptedModel([_proposal(action_id) for action_id in action_ids])


# --- Approval gateway: pause, grant, reject ------------------------------------


def test_approval_gated_action_quiesces_the_run_until_granted() -> None:
    store = InMemoryEventStore()
    model = _model("gated-1")
    runtime = _runtime(model, store=store)
    del model  # turn counting is pinned on the durable stream below

    run_id = runtime.start("repair with approval gate")
    waiting = runtime.step(run_id)

    assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
    assert waiting.current_action_id == "gated-1"
    requested = [e for e in store.events_for(run_id) if isinstance(e, ApprovalRequested)]
    assert len(requested) == 1
    assert requested[0].action_id == ActionId("gated-1")
    # Quiescent: stepping/resuming while waiting appends nothing.
    version = store.current_version(run_id)
    assert runtime.step(run_id).status is RunStatus.WAITING_FOR_APPROVAL
    assert runtime.resume(run_id).status is RunStatus.WAITING_FOR_APPROVAL
    assert store.current_version(run_id) == version

    granted = runtime.grant_approval(run_id, ActionId("gated-1"))

    assert granted.status is RunStatus.READY
    assert granted.approved_action_ids == ("gated-1",)
    assert len([e for e in store.events_for(run_id) if isinstance(e, ApprovalGranted)]) == 1

    final = runtime.resume(run_id)

    assert final.status is RunStatus.SUCCEEDED
    # The approved action executed without a second model turn or re-approval.
    events = store.events_for(run_id)
    assert len([e for e in events if isinstance(e, ActionProposed)]) == 1
    succeeded = [e for e in events if isinstance(e, ToolSucceeded)]
    assert len(succeeded) == 1
    assert len([e for e in events if isinstance(e, ApprovalRequested)]) == 1


def test_blocking_drive_returns_at_the_approval_gate_instead_of_hanging() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(_model("gated-1"), store=store)

    state = runtime.run("blocking drive must quiesce")

    assert state.status is RunStatus.WAITING_FOR_APPROVAL
    assert runtime.resume(state.run_id).status is RunStatus.WAITING_FOR_APPROVAL


def test_rejected_approval_clears_the_proposal_and_the_run_replans() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(_model("gated-1", "gated-2"), store=store)
    run_id = runtime.start("reject then steer")
    assert runtime.step(run_id).status is RunStatus.WAITING_FOR_APPROVAL

    rejected = runtime.reject_approval(run_id, ActionId("gated-1"), reason="too risky")

    assert rejected.status is RunStatus.READY
    assert rejected.current_proposal is None
    assert rejected.last_approval_rejection == "too risky"
    durable = [e for e in store.events_for(run_id) if isinstance(e, ApprovalRejected)]
    assert len(durable) == 1
    assert durable[0].reason == "too risky"

    # The next proposal is approval-gated too, so the run quiesces again —
    # the operator channel never becomes an autonomous retry loop.
    waiting = runtime.resume(run_id)
    assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
    assert waiting.current_action_id == "gated-2"
    assert len([e for e in store.events_for(run_id) if isinstance(e, ApprovalRequested)]) == 2


def test_policy_dependent_approval_fails_closed_to_the_operator() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(
        _model("gated-1"),
        store=store,
        tools=ScriptedTools(
            [ToolResult(ok=True, observation="all tests pass")],
            metadata=[_metadata("deploy", ApprovalClass.POLICY_DEPENDENT)],
        ),
    )
    run_id = runtime.start("policy dependent gate")

    assert runtime.step(run_id).status is RunStatus.WAITING_FOR_APPROVAL


def test_unapproved_tool_runs_without_the_gateway() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(
        _model("plain-1"),
        store=store,
        tools=ScriptedTools(
            [ToolResult(ok=True, observation="all tests pass")],
            metadata=[_metadata("deploy", ApprovalClass.NONE)],
        ),
    )

    state = runtime.run("no approval required")

    assert state.status is RunStatus.SUCCEEDED
    assert not [e for e in store.events_for(state.run_id) if isinstance(e, ApprovalRequested)]


# --- Approval can never expand authority ----------------------------------------


def test_permission_denial_happens_before_the_approval_gateway() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(
        _model("gated-1", "plain-read", "plain-read", "plain-read", "plain-read"),
        store=store,
        permissions=frozenset({Permission.READ}),
    )
    run_id = runtime.start("no local write permission")

    state = runtime.step(run_id)

    assert state.status is RunStatus.READY
    rejected = [e for e in store.events_for(run_id) if isinstance(e, ActionRejected)]
    assert [e.reason_code for e in rejected] == ["BLOCK_PERMISSION_DENIED"]
    assert not [e for e in store.events_for(run_id) if isinstance(e, ApprovalRequested)]


# --- Deny paths for the operator commands ---------------------------------------


def test_grant_requires_a_waiting_run_with_the_matching_action() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(_model("gated-1"), store=store)
    run_id = runtime.start("grant validation")

    with pytest.raises(InvalidTransitionError, match="not waiting for approval"):
        runtime.grant_approval(run_id, ActionId("gated-1"))

    assert runtime.step(run_id).status is RunStatus.WAITING_FOR_APPROVAL
    with pytest.raises(InvalidTransitionError, match="not waiting on action"):
        runtime.grant_approval(run_id, ActionId("other"))

    # A failed grant appends nothing: the run is still waiting on gated-1.
    assert runtime.state_for(run_id).status is RunStatus.WAITING_FOR_APPROVAL
    assert len([e for e in store.events_for(run_id) if isinstance(e, ApprovalGranted)]) == 0


def test_reject_requires_a_waiting_run_with_the_matching_action() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(_model("gated-1"), store=store)
    run_id = runtime.start("reject validation")

    with pytest.raises(InvalidTransitionError, match="not waiting for approval"):
        runtime.reject_approval(run_id, ActionId("gated-1"), reason="no")

    assert runtime.step(run_id).status is RunStatus.WAITING_FOR_APPROVAL
    with pytest.raises(InvalidTransitionError, match="not waiting on action"):
        runtime.reject_approval(run_id, ActionId("other"), reason="no")

    assert runtime.state_for(run_id).status is RunStatus.WAITING_FOR_APPROVAL
    assert len([e for e in store.events_for(run_id) if isinstance(e, ApprovalRejected)]) == 0


def test_terminal_runs_reject_operator_commands() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(
        _model("plain-1"),
        store=store,
        tools=ScriptedTools(
            [ToolResult(ok=True, observation="all tests pass")],
            metadata=[_metadata("deploy", ApprovalClass.NONE)],
        ),
    )
    state = runtime.run("terminal")
    assert state.status is RunStatus.SUCCEEDED

    with pytest.raises(InvalidTransitionError, match="not waiting for approval"):
        runtime.grant_approval(state.run_id, ActionId("plain-1"))
    with pytest.raises(InvalidTransitionError, match="terminal run"):
        runtime.add_operator_instruction(state.run_id, "too late")


# --- Process restart: durable pause, grant, resume -------------------------------


def test_paused_run_survives_process_restart_and_resumes_after_grant(
    tmp_path: Path,
) -> None:
    db = tmp_path / "events.db"
    codec = JsonEventCodec()
    first_store = SQLiteEventStore(db, codec=codec)
    first = _runtime(_model("gated-1"), store=first_store)
    run_id = first.start("durable pause")
    assert first.step(run_id).status is RunStatus.WAITING_FOR_APPROVAL

    # A fresh runtime on the same store is a different "process": no in-memory
    # drive state, only the authoritative stream.
    second = _runtime(_model("gated-1"), store=SQLiteEventStore(db, codec=codec))
    assert second.state_for(run_id).status is RunStatus.WAITING_FOR_APPROVAL
    assert second.resume(run_id).status is RunStatus.WAITING_FOR_APPROVAL

    granted = second.grant_approval(run_id, ActionId("gated-1"))
    assert granted.status is RunStatus.READY

    final = second.resume(run_id)
    assert final.status is RunStatus.SUCCEEDED
    assert final.stop_reason is StopReason.SUCCESS_VERIFIED


def test_rejection_survives_process_restart(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    codec = JsonEventCodec()
    first = _runtime(_model("gated-1"), store=SQLiteEventStore(db, codec=codec))
    run_id = first.start("durable reject")
    assert first.step(run_id).status is RunStatus.WAITING_FOR_APPROVAL

    second = _runtime(_model("gated-2"), store=SQLiteEventStore(db, codec=codec))
    rejected = second.reject_approval(run_id, ActionId("gated-1"), reason="denied after restart")
    assert rejected.status is RunStatus.READY
    assert rejected.last_approval_rejection == "denied after restart"

    waiting = second.resume(run_id)
    assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
    assert waiting.current_action_id == "gated-2"


# --- Operator instructions -------------------------------------------------------


def test_operator_instruction_is_durable_and_can_amend_the_objective() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(_model("gated-1"), store=store)
    run_id = runtime.start("original objective")

    steered = runtime.add_operator_instruction(run_id, "only fix adder.py", amend_objective=True)

    assert steered.objective == "only fix adder.py"
    assert steered.operator_instructions == ("only fix adder.py",)
    events = [e for e in store.events_for(run_id) if isinstance(e, OperatorInstruction)]
    assert len(events) == 1
    assert events[0].amends_objective is True

    plain = runtime.add_operator_instruction(run_id, "prefer small diffs")
    assert plain.objective == "only fix adder.py"
    assert plain.operator_instructions == ("only fix adder.py", "prefer small diffs")


def test_operator_instruction_is_accepted_while_waiting_for_approval() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(_model("gated-1"), store=store)
    run_id = runtime.start("steer while paused")
    assert runtime.step(run_id).status is RunStatus.WAITING_FOR_APPROVAL

    steered = runtime.add_operator_instruction(run_id, "hold off on deploy")

    assert steered.status is RunStatus.WAITING_FOR_APPROVAL
    assert steered.operator_instructions == ("hold off on deploy",)


def test_operator_instruction_requires_a_quiescent_run() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(_model("gated-1"), store=store)
    run_id = runtime.start("acting denial")
    # Drive the stream into ACTING by appending the authorized action directly
    # (a live drive never rests in ACTING between events).
    proposal = _proposal("act-1")
    version = store.current_version(run_id)
    store.append(
        ActionProposed(
            event_id=EventId("test-proposed"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 1,
            proposal=proposal,
        ),
        expected_version=version,
    )
    store.append(
        ActionAuthorized(
            event_id=EventId("test-authorized"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 2,
            proposal=proposal,
            tool_metadata=_metadata("deploy", ApprovalClass.REQUIRED),
        ),
        expected_version=version + 1,
    )

    with pytest.raises(InvalidTransitionError, match="pause the run first"):
        runtime.add_operator_instruction(run_id, "not while acting")


def test_operator_instruction_on_an_unknown_run_fails_closed() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(_model("gated-1"), store=store)

    with pytest.raises(LookupError, match="no persisted run"):
        runtime.add_operator_instruction(RunId("missing"), "nothing here")


def test_spent_grant_is_not_re_executed_after_failed_verification() -> None:
    """Regression pin for the PACS-014 live round-trip defect.

    An approved action that executes successfully but fails verification
    re-plans back to READY with the stale proposal still projected. The grant
    must be spent by the successful execution — otherwise the next cycle
    re-executes the same approved action at attempt 1, persisting an event
    the reducer rejects and durably poisoning the stream.
    """
    store = InMemoryEventStore()
    runtime = _runtime(
        _model("turn-1", "turn-2"),
        store=store,
        tools=ScriptedTools(
            [
                ToolResult(ok=True, observation="still failing"),
                ToolResult(ok=True, observation="all tests pass"),
            ],
            metadata=[_metadata("deploy", ApprovalClass.REQUIRED)],
        ),
    )
    run_id = runtime.start("approved edit, failed verification")
    assert runtime.step(run_id).status is RunStatus.WAITING_FOR_APPROVAL
    runtime.grant_approval(run_id, ActionId("turn-1"))

    ready = runtime.step(run_id)  # executes turn-1; verification fails; re-plans

    assert ready.status is RunStatus.READY
    # The grant is spent by the successful execution.
    assert ready.approved_action_ids == ()
    succeeded = [e for e in store.events_for(run_id) if isinstance(e, ToolSucceeded)]
    assert [(e.action_id, e.attempt) for e in succeeded] == [(ActionId("turn-1"), 1)]

    # The next cycle proposes a FRESH action instead of re-executing the stale
    # approved one — and the fresh gated action re-quiesces the run.
    waiting = runtime.step(run_id)

    assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
    assert waiting.current_action_id == "turn-2"
    events = store.events_for(run_id)
    started = [e for e in events if isinstance(e, ToolExecutionStarted)]
    assert [(e.action_id, e.attempt) for e in started] == [(ActionId("turn-1"), 1)]
    assert len([e for e in events if isinstance(e, ApprovalRequested)]) == 2
    # The stream stays fully replayable from a fresh runtime (poison guard).
    fresh = _runtime(_model(), store=store)
    assert fresh.state_for(run_id).status is RunStatus.WAITING_FOR_APPROVAL
