"""Counterfactual replay tests (PACS-017 M4).

The re-drive seam is pinned end to end: recorded decisions are extracted
faithfully; clean resume points reproduce the historical suffix exactly
(MATCHED); candidate-policy knobs and genuine resume semantics surface as
honest DIVERGED outcomes; undeterminable re-drives (non-idempotent
in-flight side effects, exhausted recorded turns) are DIVERGED_UNKNOWN,
never smoothed over; corrupted streams fail closed; and evidence-only
shadow records can never manufacture a divergence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from loopforge.adapters.context import BudgetedContextBuilder, CharsPerTokenCounter
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
    ScriptedVerifier,
)
from loopforge.application.counterfactual import (
    CounterfactualPrefixError,
    CounterfactualResult,
    ReplayOutcome,
    counterfactual_redrive,
    extract_scripted_turns,
)
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.events import (
    ActionProposed,
    ApprovalGranted,
    ApprovalRequested,
    Event,
    PlanCreated,
    ShadowDecisionRecorded,
    ToolExecutionStarted,
    ToolSucceeded,
    VerificationPassed,
)
from loopforge.domain.policies import (
    ContextAllocationBounds,
    ExecutionPolicy,
    ShadowDecisionKind,
)
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy, ToolFailureClass
from loopforge.domain.state import RunState, replay
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
)
from loopforge.entrypoints.replay import build_counterfactual_runtime
from loopforge.ports.shadow import ShadowAdvice
from loopforge.ports.tools import ToolResult
from loopforge.ports.verifier import VerificationResult

NOW = datetime(2026, 9, 5, tzinfo=UTC)
BUDGET = BudgetLimit(max_cost_usd=5.0, max_iterations=8)

CANDIDATE = ExecutionPolicy(
    policy_id="replay-candidate",
    version=1,
    context_allocation=ContextAllocationBounds(floor_tokens=1024, ceiling_tokens=4096),
)


def _metadata(  # noqa: PLR0913 - tool-contract knobs stay explicit at each call site
    name: str = "inspect",
    *,
    permission: Permission = Permission.READ,
    risk: RiskLevel = RiskLevel.READ_ONLY,
    side_effect: SideEffectClass = SideEffectClass.READ_ONLY,
    idempotency: IdempotencyClass = IdempotencyClass.NATURAL,
    approval: ApprovalClass = ApprovalClass.NONE,
) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=risk,
        required_permission=permission,
        side_effect=side_effect,
        retry=RetryClass.NEVER,
        idempotency=idempotency,
        approval=approval,
        timeout_seconds=5.0,
        sensitivity=DataSensitivity.INTERNAL,
    )


def _permissions_for(metadata: list[ToolMetadata]) -> PermissionPolicy:
    return PermissionPolicy(frozenset({item.required_permission for item in metadata}))


def _drive_run(  # noqa: PLR0913 - run-wiring knobs stay explicit at each call site
    *,
    proposals: list[ActionProposal] | None = None,
    results: list[ToolResult] | None = None,
    metadata: list[ToolMetadata] | None = None,
    verify_read_only_turns: bool = True,
    budget: BudgetLimit = BUDGET,
    shadow: object | None = None,
) -> tuple[tuple[Event, ...], RunId]:
    """Drive one scripted run to settlement; return its authoritative stream."""
    tools_metadata = metadata if metadata is not None else [_metadata()]
    store = InMemoryEventStore()
    runtime = Runtime(
        model=ScriptedModel(proposals or [ActionProposal(ActionId("a1"), "inspect", {})]),
        tools=ScriptedTools(
            results or [ToolResult(ok=True, observation="all done")],
            metadata=tools_metadata,
        ),
        verifier=ObservationContainsVerifier("done"),
        store=store,
        control=ControlPolicy(budget),
        permissions=_permissions_for(tools_metadata),
        reliability=ReliabilityPolicy(),
        context=BudgetedContextBuilder(
            FixedClock(NOW),
            CharsPerTokenCounter(),
            template=default_controller_template(),
            token_budget=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
        ),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
        verify_read_only_turns=verify_read_only_turns,
        shadow=shadow,  # pyright: ignore[reportArgumentType]
    )
    state = runtime.run("counterfactual probe")
    return store.events_for(state.run_id), state.run_id


def _two_turn_run() -> tuple[tuple[Event, ...], RunId]:
    """A run whose first verification fails and whose second succeeds."""
    return _drive_run(
        proposals=[
            ActionProposal(ActionId("a1"), "inspect", {}),
            ActionProposal(ActionId("a2"), "inspect", {}),
        ],
        results=[
            ToolResult(ok=True, observation="nothing yet"),
            ToolResult(ok=True, observation="all done"),
        ],
    )


def _redrive(
    historical: tuple[Event, ...],
    run_id: RunId,
    *,
    prefix_length: int,
    policy: ExecutionPolicy | None = None,
) -> CounterfactualResult:
    runtime = build_counterfactual_runtime(
        historical, store=InMemoryEventStore(), budget=BUDGET, policy=policy
    )
    return counterfactual_redrive(
        runtime=runtime, run_id=run_id, historical=historical, prefix_length=prefix_length
    )


def _prefix_after(historical: tuple[Event, ...], event_type: type, occurrence: int = 1) -> int:
    """Stream prefix length ending right after the Nth event of a type."""
    seen = 0
    for index, event in enumerate(historical, 1):
        if isinstance(event, event_type):
            seen += 1
            if seen == occurrence:
                return index
    msg = f"stream has no {occurrence}th {event_type.__name__}"
    raise AssertionError(msg)


# --- Extraction -----------------------------------------------------------------


def test_extract_replays_the_recorded_decisions_in_stream_order() -> None:
    historical, _ = _two_turn_run()
    turns = extract_scripted_turns(historical)
    assert [proposal.action_id for proposal in turns.proposals] == [
        ActionId("a1"),
        ActionId("a2"),
    ]
    assert [result.observation for result in turns.tool_results] == [
        "nothing yet",
        "all done",
    ]
    assert [verification.passed for verification in turns.verifications] == [False, True]
    # Two authorizations of the same tool collapse to one contract record.
    assert [item.name for item in turns.tool_metadata] == ["inspect"]


def test_extract_reconstructs_failed_tool_results() -> None:
    historical, _ = _drive_run(
        proposals=[
            ActionProposal(ActionId("a1"), "inspect", {}),
            ActionProposal(ActionId("a2"), "inspect", {}),
        ],
        results=[
            ToolResult(
                ok=False,
                observation="tool exploded",
                error_code="E_BOOM",
                failure_class=ToolFailureClass.PERMANENT,
            ),
            ToolResult(ok=True, observation="all done"),
        ],
    )
    result = extract_scripted_turns(historical).tool_results[0]
    assert result == ToolResult(
        ok=False,
        observation="tool exploded",
        error_code="E_BOOM",
        failure_class=ToolFailureClass.PERMANENT,
    )


def test_extract_from_an_empty_stream_yields_no_turns() -> None:
    turns = extract_scripted_turns(())
    assert turns.proposals == ()
    assert turns.tool_results == ()
    assert turns.tool_metadata == ()
    assert turns.verifications == ()


# --- MATCHED: clean resume points reproduce the suffix --------------------------


def test_redrive_from_a_clean_ready_prefix_matches_the_historical_suffix() -> None:
    historical, run_id = _two_turn_run()
    result = _redrive(historical, run_id, prefix_length=_prefix_after(historical, PlanCreated))
    assert result.outcome is ReplayOutcome.MATCHED
    assert result.historical_status is RunStatus.SUCCEEDED
    assert result.redriven_status is RunStatus.SUCCEEDED
    assert result.redriven_length == result.historical_length
    assert result.first_divergence_sequence is None


def test_redrive_from_acting_re_executes_an_idempotent_tool_identically() -> None:
    historical, run_id = _drive_run()
    result = _redrive(
        historical, run_id, prefix_length=_prefix_after(historical, ToolExecutionStarted)
    )
    assert result.outcome is ReplayOutcome.MATCHED
    assert result.redriven_status is RunStatus.SUCCEEDED


def test_redrive_from_verifying_replays_the_recorded_verdict() -> None:
    historical, run_id = _drive_run()
    result = _redrive(historical, run_id, prefix_length=_prefix_after(historical, ToolSucceeded))
    assert result.outcome is ReplayOutcome.MATCHED


def test_redrive_from_a_terminal_prefix_is_trivially_matched() -> None:
    historical, run_id = _drive_run()
    result = _redrive(historical, run_id, prefix_length=len(historical))
    assert result.outcome is ReplayOutcome.MATCHED
    assert result.redriven_status is RunStatus.SUCCEEDED
    assert result.redriven_length == len(historical)


def test_shadow_evidence_never_manufactures_a_divergence() -> None:
    class FixedAdvisor:
        @property
        def policy(self) -> ExecutionPolicy:
            return CANDIDATE

        def advise_route(self, state: object, *, signals: object) -> ShadowAdvice:
            del state, signals
            return ShadowAdvice(
                kind=ShadowDecisionKind.MODEL_ROUTE,
                decision="scripted/scripted-deterministic tier=economy",
                basis="reason_code=ROUTE_INITIAL_SELECTION",
            )

        def advise_context_budget(self, state: object) -> ShadowAdvice:
            del state
            return ShadowAdvice(
                kind=ShadowDecisionKind.CONTEXT_BUDGET,
                decision="max_tokens=4096 reserve_tokens=256",
                basis="accounting=absent",
            )

        def advise_verification_cadence(self, state: object) -> ShadowAdvice:
            del state
            return ShadowAdvice(
                kind=ShadowDecisionKind.VERIFICATION_CADENCE,
                decision="verify",
                basis="verify_read_only_turns=True",
            )

    historical, run_id = _drive_run(shadow=FixedAdvisor())
    assert any(isinstance(event, ShadowDecisionRecorded) for event in historical)
    result = _redrive(historical, run_id, prefix_length=_prefix_after(historical, PlanCreated))
    assert result.outcome is ReplayOutcome.MATCHED


# --- DIVERGED: honest counterfactual findings -----------------------------------


def test_candidate_context_policy_diverges_at_the_budget_pressure_point() -> None:
    # The candidate's tighter context ceiling drops content the historical
    # envelope kept; every other decision replays identically, so the runs
    # settle alike while the assembled contexts honestly diverge.
    historical, run_id = _drive_run(
        proposals=[
            ActionProposal(ActionId("a1"), "inspect", {}),
            ActionProposal(ActionId("a2"), "inspect", {}),
        ],
        results=[
            ToolResult(ok=True, observation="x" * 8000),
            ToolResult(ok=True, observation="all done"),
        ],
    )
    candidate = ExecutionPolicy(
        policy_id="tight-context-candidate",
        version=1,
        context_allocation=ContextAllocationBounds(
            floor_tokens=1024, ceiling_tokens=2048, reserve_tokens=256
        ),
        verify_read_only_turns=True,
    )
    result = _redrive(
        historical,
        run_id,
        prefix_length=_prefix_after(historical, PlanCreated),
        policy=candidate,
    )
    assert result.outcome is ReplayOutcome.DIVERGED
    assert "ContextAssembled" in result.detail
    assert "'context_items'" in result.detail
    assert result.historical_status is RunStatus.SUCCEEDED
    assert result.redriven_status is RunStatus.SUCCEEDED


def test_candidate_cadence_beyond_recorded_turns_is_honest_unknown() -> None:
    # The candidate skips the read-only verification that ended the
    # historical run, so the re-drive needs turns the record never held:
    # the counterfactual future is unknowable and reported as such.
    historical, run_id = _drive_run()
    candidate = ExecutionPolicy(
        policy_id="cadence-candidate",
        version=1,
        context_allocation=ContextAllocationBounds(
            floor_tokens=1024, ceiling_tokens=4096, reserve_tokens=256
        ),
        verify_read_only_turns=False,
    )
    result = _redrive(
        historical,
        run_id,
        prefix_length=_prefix_after(historical, PlanCreated),
        policy=candidate,
    )
    assert result.outcome is ReplayOutcome.DIVERGED_UNKNOWN
    assert result.redriven_status is None
    assert "scripted model exhausted" in result.detail


def test_mid_turn_ready_prefixes_repropose_under_resume_semantics() -> None:
    # Resume from a READY point with the turn already underway re-runs the
    # whole proposal half-cycle (context, model, proposal): that is the
    # runtime's genuine crash-resume behavior, reported as an honest
    # divergence — never silently treated as a continuation.
    historical, run_id = _drive_run()
    for prefix in (
        _prefix_after(historical, ActionProposed),
        _prefix_after(historical, PlanCreated) + 1,
    ):
        result = _redrive(historical, run_id, prefix_length=prefix)
        assert result.outcome is ReplayOutcome.DIVERGED
        assert result.first_divergence_sequence == prefix + 1
        assert "ContextAssembled" in result.detail


def test_planning_prefix_diverges_at_the_resume_replan_and_names_the_field() -> None:
    historical, run_id = _drive_run()
    result = _redrive(historical, run_id, prefix_length=1)
    assert result.outcome is ReplayOutcome.DIVERGED
    assert result.first_divergence_sequence == 2
    assert "PlanCreated vs redriven PlanCreated" in result.detail
    assert "'plan'" in result.detail


def test_approval_gate_prefix_cannot_self_approve() -> None:
    gated = _metadata(approval=ApprovalClass.REQUIRED)
    store = InMemoryEventStore()
    runtime = Runtime(
        model=ScriptedModel([ActionProposal(ActionId("a1"), "inspect", {})]),
        tools=ScriptedTools([ToolResult(ok=True, observation="all done")], metadata=[gated]),
        verifier=ObservationContainsVerifier("done"),
        store=store,
        control=ControlPolicy(BUDGET),
        permissions=_permissions_for([gated]),
        reliability=ReliabilityPolicy(),
        context=BudgetedContextBuilder(
            FixedClock(NOW),
            CharsPerTokenCounter(),
            template=default_controller_template(),
            token_budget=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
        ),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
        verify_read_only_turns=True,
    )
    waiting = runtime.run("approval probe")
    assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
    runtime.grant_approval(waiting.run_id, ActionId("a1"))
    settled = runtime.resume(waiting.run_id)
    assert settled.status is RunStatus.SUCCEEDED
    historical = store.events_for(waiting.run_id)
    result = _redrive(
        historical,
        waiting.run_id,
        prefix_length=_prefix_after(historical, ApprovalRequested),
    )
    assert result.outcome is ReplayOutcome.DIVERGED
    assert result.first_divergence_sequence == _prefix_after(historical, ApprovalGranted)
    assert result.redriven_status is RunStatus.WAITING_FOR_APPROVAL


def test_a_run_left_waiting_for_approval_matches_its_own_quiescence() -> None:
    gated = _metadata(approval=ApprovalClass.REQUIRED)
    historical, run_id = _drive_run(metadata=[gated])
    assert replay(run_id, historical).status is RunStatus.WAITING_FOR_APPROVAL
    result = _redrive(historical, run_id, prefix_length=len(historical))
    assert result.outcome is ReplayOutcome.MATCHED
    assert result.redriven_status is RunStatus.WAITING_FOR_APPROVAL


# --- DIVERGED_UNKNOWN: honest absence of determinism ----------------------------


def test_acting_prefix_with_a_non_idempotent_action_is_honest_unknown() -> None:
    unsafe = _metadata(
        permission=Permission.LOCAL_WRITE,
        risk=RiskLevel.LOCAL_WRITE,
        side_effect=SideEffectClass.LOCAL_WRITE,
        idempotency=IdempotencyClass.NONE,
    )
    historical, run_id = _drive_run(metadata=[unsafe])
    result = _redrive(
        historical, run_id, prefix_length=_prefix_after(historical, ToolExecutionStarted)
    )
    assert result.outcome is ReplayOutcome.DIVERGED_UNKNOWN
    assert result.redriven_status is None
    assert "resume safely" in result.detail


def test_verifier_exhaustion_is_honest_unknown() -> None:
    # History skipped every read-only verification (cadence knob off) and
    # settled on the iteration budget; the legacy-cadence re-drive must
    # verify the first turn, but no verdict was ever recorded.
    skip_history_budget = BudgetLimit(max_cost_usd=5.0, max_iterations=3)
    historical, run_id = _drive_run(
        proposals=[ActionProposal(ActionId(f"a{index}"), "inspect", {}) for index in range(5)],
        results=[ToolResult(ok=True, observation="nothing yet") for _ in range(5)],
        verify_read_only_turns=False,
        budget=skip_history_budget,
    )
    assert not any(isinstance(event, VerificationPassed) for event in historical)
    result = _redrive(historical, run_id, prefix_length=_prefix_after(historical, PlanCreated))
    assert result.outcome is ReplayOutcome.DIVERGED_UNKNOWN
    assert result.redriven_status is None
    assert "exhausted" in result.detail


def test_model_exhaustion_is_honest_unknown() -> None:
    historical, run_id = _drive_run()
    turns = extract_scripted_turns(historical)
    runtime = Runtime(
        model=ScriptedModel([]),
        tools=ScriptedTools(list(turns.tool_results), metadata=list(turns.tool_metadata)),
        verifier=ScriptedVerifier(list(turns.verifications)),
        store=InMemoryEventStore(),
        control=ControlPolicy(BUDGET),
        permissions=_permissions_for(list(turns.tool_metadata)),
        reliability=ReliabilityPolicy(),
        context=BudgetedContextBuilder(
            FixedClock(NOW),
            CharsPerTokenCounter(),
            template=default_controller_template(),
            token_budget=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
        ),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
        verify_read_only_turns=True,
    )
    result = counterfactual_redrive(
        runtime=runtime,
        run_id=run_id,
        historical=historical,
        prefix_length=_prefix_after(historical, PlanCreated),
    )
    assert result.outcome is ReplayOutcome.DIVERGED_UNKNOWN
    assert "scripted model exhausted" in result.detail


# --- Fail-closed validation ------------------------------------------------------


def test_corrupted_stream_fails_closed() -> None:
    historical, run_id = _drive_run()
    gap = historical[:4] + historical[5:]
    with pytest.raises(CounterfactualPrefixError, match="domain validation"):
        _redrive(gap, run_id, prefix_length=2)


def test_empty_stream_fails_closed() -> None:
    historical, _ = _drive_run()
    runtime = build_counterfactual_runtime(historical, store=InMemoryEventStore(), budget=BUDGET)
    with pytest.raises(CounterfactualPrefixError, match="empty"):
        counterfactual_redrive(
            runtime=runtime, run_id=RunId("no-such-run"), historical=(), prefix_length=1
        )


@pytest.mark.parametrize("prefix_length", [0, -1, 10_000])
def test_prefix_length_out_of_bounds_fails_closed(prefix_length: int) -> None:
    historical, run_id = _drive_run()
    with pytest.raises(ValueError, match="prefix_length"):
        _redrive(historical, run_id, prefix_length=prefix_length)


def test_a_non_fresh_store_fails_closed() -> None:
    historical, run_id = _drive_run()
    store = InMemoryEventStore()
    runtime = build_counterfactual_runtime(historical, store=store, budget=BUDGET)
    store.append(historical[0], expected_version=0)
    with pytest.raises(ValueError, match="fresh store"):
        counterfactual_redrive(
            runtime=runtime, run_id=run_id, historical=historical, prefix_length=2
        )


def test_result_invariants_are_enforced() -> None:
    base: dict[str, Any] = {
        "run_id": RunId("r"),
        "prefix_length": 1,
        "historical_length": 2,
        "redriven_length": 2,
        "historical_status": RunStatus.SUCCEEDED,
        "detail": "honest explanation",
    }
    with pytest.raises(ValueError, match="first divergence"):
        CounterfactualResult(
            **base,
            outcome=ReplayOutcome.DIVERGED,
            redriven_status=RunStatus.SUCCEEDED,
            first_divergence_sequence=None,
        )
    with pytest.raises(ValueError, match="no honest redriven status"):
        CounterfactualResult(
            **base,
            outcome=ReplayOutcome.DIVERGED_UNKNOWN,
            redriven_status=RunStatus.SUCCEEDED,
            first_divergence_sequence=None,
        )
    with pytest.raises(ValueError, match="only a diverged outcome"):
        CounterfactualResult(
            **base,
            outcome=ReplayOutcome.MATCHED,
            redriven_status=RunStatus.SUCCEEDED,
            first_divergence_sequence=2,
        )


# --- Builder + scripted verifier -------------------------------------------------


def test_builder_rejects_an_empty_stream() -> None:
    with pytest.raises(ValueError, match="empty stream"):
        build_counterfactual_runtime((), store=InMemoryEventStore(), budget=BUDGET)


def test_builder_enforces_the_context_envelope_against_candidate_bounds() -> None:
    historical, _ = _drive_run()
    greedy = ExecutionPolicy(
        policy_id="greedy-candidate",
        version=1,
        context_allocation=ContextAllocationBounds(
            floor_tokens=1024, ceiling_tokens=8192, reserve_tokens=256
        ),
    )
    with pytest.raises(ValueError, match="envelope"):
        build_counterfactual_runtime(
            historical, store=InMemoryEventStore(), budget=BUDGET, policy=greedy
        )


def test_scripted_verifier_replays_in_order_then_fails_closed() -> None:
    verifier = ScriptedVerifier(
        [
            VerificationResult(passed=False, summary="not yet"),
            VerificationResult(passed=True, summary="done"),
        ]
    )
    state = RunState(run_id=RunId("r"))
    assert verifier.verify(state).passed is False
    assert verifier.verify(state).passed is True
    with pytest.raises(RuntimeError, match="exhausted"):
        verifier.verify(state)
