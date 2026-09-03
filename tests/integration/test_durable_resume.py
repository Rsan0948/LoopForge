from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.context import (
    BasicContextBuilder,
    BudgetedContextBuilder,
    CharsPerTokenCounter,
)
from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.scripted import ObservationContainsVerifier, ScriptedModel, ScriptedTools
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.application.runtime import Runtime, UnsafeResumeStateError
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    BudgetDebited,
    ContextAssembled,
    Event,
    ToolExecutionStarted,
    ToolSucceeded,
)
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy
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
    RunStatus,
    UsageDelta,
)
from loopforge.ports.tools import ToolResult

NOW = datetime(2026, 8, 22, tzinfo=UTC)


def _metadata() -> ToolMetadata:
    return ToolMetadata(
        name="inspect",
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _runtime(path: Path, *, action_id: str = "a1") -> Runtime:
    return Runtime(
        model=ScriptedModel([ActionProposal(ActionId(action_id), "inspect", {})]),
        tools=ScriptedTools(
            [ToolResult(ok=True, observation="all tests pass")],
            metadata=[_metadata()],
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=SQLiteEventStore(path, codec=JsonEventCodec()),
        control=ControlPolicy(BudgetLimit(5.0, 10)),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(),
        context=BasicContextBuilder(SystemClock()),
        clock=SystemClock(),
        sleeper=SystemSleeper(),
        # Legacy cadence (PACS-016 M8): success is granted by a READ-class
        # tool's verification; these pins exercise durable resume, not cadence.
        verify_read_only_turns=True,
    )


def test_run_can_resume_in_fresh_runtime_process_boundary(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    first_runtime = _runtime(path, action_id="unused")
    run_id = first_runtime.start("repair auth")
    checkpoint = first_runtime.state_for(run_id)
    assert checkpoint.status is RunStatus.READY
    assert checkpoint.version == 2

    # Simulate process restart by constructing a new store and runtime instance.
    second_runtime = _runtime(path, action_id="after-restart")
    completed = second_runtime.resume(run_id)

    assert completed.status is RunStatus.SUCCEEDED
    assert completed.iteration == 1
    assert completed.last_observation == "all tests pass"
    assert second_runtime.state_for(run_id) == completed


def test_resume_fails_closed_from_ambiguous_acting_state(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    runtime = _runtime(path)
    run_id = runtime.start("repair auth")
    store = runtime.store
    proposal = ActionProposal(ActionId("ambiguous"), "inspect", {})

    version = store.current_version(run_id)
    store.append(
        BudgetDebited(
            event_id=EventId("manual-budget"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 1,
            usage=UsageDelta(cost_usd=0.01, input_tokens=1, output_tokens=1),
        ),
        expected_version=version,
    )
    version = store.current_version(run_id)
    store.append(
        ActionProposed(
            event_id=EventId("manual-proposed"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 1,
            proposal=proposal,
        ),
        expected_version=version,
    )
    version = store.current_version(run_id)
    store.append(
        ActionAuthorized(
            event_id=EventId("manual-authorized"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 1,
            proposal=proposal,
            tool_metadata=ToolMetadata(
                name="inspect",
                risk=RiskLevel.LOCAL_WRITE,
                required_permission=Permission.LOCAL_WRITE,
                side_effect=SideEffectClass.LOCAL_WRITE,
                retry=RetryClass.NEVER,
                idempotency=IdempotencyClass.NONE,
                approval=ApprovalClass.NONE,
                timeout_seconds=5.0,
            ),
        ),
        expected_version=version,
    )
    version = store.current_version(run_id)
    store.append(
        ToolExecutionStarted(
            event_id=EventId("manual-started"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 1,
            action_id=proposal.action_id,
            attempt=1,
            idempotency_key=None,
        ),
        expected_version=version,
    )

    assert runtime.state_for(run_id).status is RunStatus.ACTING
    with pytest.raises(UnsafeResumeStateError, match="without idempotency"):
        _runtime(path).resume(run_id)


def test_resume_from_verifying_checkpoint_does_not_repeat_tool_side_effect(tmp_path: Path) -> None:
    path = tmp_path / "verify-resume.db"
    runtime = _runtime(path)
    run_id = runtime.start("repair auth")
    store = runtime.store
    proposal = ActionProposal(ActionId("already-executed"), "inspect", {})

    def append(factory: Callable[[int], Event]) -> None:
        version = store.current_version(run_id)
        store.append(factory(version + 1), expected_version=version)

    append(
        lambda sequence: BudgetDebited(
            event_id=EventId("verify-budget"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            usage=UsageDelta(cost_usd=0.01, input_tokens=1, output_tokens=1),
        )
    )
    append(
        lambda sequence: ActionProposed(
            event_id=EventId("verify-proposed"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            proposal=proposal,
        )
    )
    append(
        lambda sequence: ActionAuthorized(
            event_id=EventId("verify-authorized"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            proposal=proposal,
            tool_metadata=_metadata(),
        )
    )
    append(
        lambda sequence: ToolSucceeded(
            event_id=EventId("verify-tool-result"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=sequence,
            action_id=proposal.action_id,
            observation="all tests pass",
        )
    )

    assert runtime.state_for(run_id).status is RunStatus.VERIFYING

    # Fresh runtime has an empty tool-result queue. If resume incorrectly
    # re-executes the tool, ScriptedTools would raise instead of succeeding.
    resumed = _runtime(path, action_id="must-not-run").resume(run_id)
    assert resumed.status is RunStatus.SUCCEEDED
    assert resumed.iteration == 1
    assert resumed.last_observation == "all tests pass"


# --- PACS-007: prompt template metadata survives durable persistence ------------


def test_budgeted_context_metadata_survives_sqlite_persistence_and_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.db"
    runtime = Runtime(
        model=ScriptedModel([ActionProposal(ActionId("a1"), "inspect", {})]),
        tools=ScriptedTools(
            [ToolResult(ok=True, observation="all tests pass")],
            metadata=[_metadata()],
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=SQLiteEventStore(path, codec=JsonEventCodec()),
        control=ControlPolicy(BudgetLimit(5.0, 10)),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(),
        context=BudgetedContextBuilder(
            SystemClock(),
            CharsPerTokenCounter(),
            template=default_controller_template(),
            token_budget=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
        ),
        clock=SystemClock(),
        sleeper=SystemSleeper(),
        # Legacy cadence (PACS-016 M8): success is granted by a READ-class
        # tool's verification; this pin exercises durable context metadata.
        verify_read_only_turns=True,
    )

    state = runtime.run("repair auth")

    assert state.status is RunStatus.SUCCEEDED
    # A fresh runtime over the same database replays the durable stream,
    # including the prompt template execution metadata.
    fresh = _runtime(path, action_id="unused")
    replayed_events = fresh.store.events_for(state.run_id)
    assembled = [event for event in replayed_events if isinstance(event, ContextAssembled)]
    assert len(assembled) == 1
    assert assembled[0].prompt_template_id == "loopforge.controller"
    assert assembled[0].prompt_template_version == "1.0.0"
    replayed = fresh.state_for(state.run_id)
    assert replayed.status is RunStatus.SUCCEEDED
    assert replayed.last_context_items == assembled[0].context_items


def test_resume_from_verifying_with_failing_verifier_does_not_wedge(tmp_path: Path) -> None:
    """Pin the REFLECTING-entry wedge: resume must re-plan, not poison the stream.

    A crash after ``ToolSucceeded`` (VERIFYING) followed by a *failing*
    verification on resume lands the run in REFLECTING. Previously the drive
    loop persisted ``ContextAssembled`` without first re-planning to READY,
    durably appending an illegal event that wedged every future replay.
    """
    path = tmp_path / "reflecting-resume.db"
    runtime = _runtime(path)
    run_id = runtime.start("repair auth")
    store = runtime.store
    proposal = ActionProposal(ActionId("a1"), "inspect", {})

    version = store.current_version(run_id)
    store.append(
        ActionProposed(
            event_id=EventId("manual-proposed"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 1,
            proposal=proposal,
        ),
        expected_version=version,
    )
    version = store.current_version(run_id)
    store.append(
        ActionAuthorized(
            event_id=EventId("manual-authorized"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 1,
            proposal=proposal,
            tool_metadata=_metadata(),
        ),
        expected_version=version,
    )
    version = store.current_version(run_id)
    store.append(
        ToolExecutionStarted(
            event_id=EventId("manual-started"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 1,
            action_id=proposal.action_id,
            attempt=1,
            idempotency_key=None,
        ),
        expected_version=version,
    )
    version = store.current_version(run_id)
    store.append(
        ToolSucceeded(
            event_id=EventId("manual-succeeded"),
            run_id=run_id,
            occurred_at=NOW,
            sequence=version + 1,
            action_id=proposal.action_id,
            observation="tests still failing",
            attempt=1,
        ),
        expected_version=version,
    )
    assert runtime.state_for(run_id).status is RunStatus.VERIFYING

    # A fresh runtime resumes: verification fails (observation lacks the
    # expected text) → REFLECTING → the drive loop must re-plan to READY and
    # continue, not append an illegal ContextAssembled.
    resumed = Runtime(
        model=ScriptedModel([ActionProposal(ActionId("a2"), "inspect", {})]),
        tools=ScriptedTools(
            [ToolResult(ok=True, observation="all tests pass")],
            metadata=[_metadata()],
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=SQLiteEventStore(path, codec=JsonEventCodec()),
        control=ControlPolicy(BudgetLimit(5.0, 10)),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(),
        context=BasicContextBuilder(SystemClock()),
        clock=SystemClock(),
        sleeper=SystemSleeper(),
        # Legacy cadence (PACS-016 M8): success is granted by a READ-class
        # tool's verification; this pin exercises resume-from-VERIFYING.
        verify_read_only_turns=True,
    ).resume(run_id)

    assert resumed.status is RunStatus.SUCCEEDED
    # The stream stays replayable in a third process boundary.
    assert _runtime(path, action_id="unused").state_for(run_id).status is RunStatus.SUCCEEDED
