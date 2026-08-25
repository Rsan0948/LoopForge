from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.state import RunState
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import (
    BudgetLimit,
    ControlDecisionKind,
    Permission,
    RiskLevel,
    RunId,
    StopReason,
)

NOW = datetime(2026, 8, 22, tzinfo=UTC)
RUN = RunId("policy-run")

_BASE_STATE = RunState(run_id=RUN)
_BASE_BUDGET = BudgetLimit(max_cost_usd=5.0, max_iterations=10)

_RISK_BY_PERMISSION = {
    Permission.READ: RiskLevel.READ_ONLY,
    Permission.LOCAL_WRITE: RiskLevel.LOCAL_WRITE,
    Permission.EXTERNAL_WRITE: RiskLevel.EXTERNAL_WRITE,
    Permission.CRITICAL: RiskLevel.CRITICAL,
}


def _state(**fields: object) -> RunState:
    return dataclasses.replace(_BASE_STATE, **fields)


def _policy(no_progress_limit: int = 3, **budget_fields: object) -> ControlPolicy:
    return ControlPolicy(
        budget=dataclasses.replace(_BASE_BUDGET, **budget_fields),
        no_progress_limit=no_progress_limit,
    )


def _tool(permission: Permission) -> ToolMetadata:
    return ToolMetadata(
        name=f"tool-{permission.value}",
        risk=_RISK_BY_PERMISSION[permission],
        required_permission=permission,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


# --- PermissionPolicy ------------------------------------------------------


def test_permission_policy_authorizes_granted_permission() -> None:
    policy = PermissionPolicy(granted=frozenset({Permission.READ, Permission.LOCAL_WRITE}))
    assert policy.authorizes(_tool(Permission.READ)) is True
    assert policy.authorizes(_tool(Permission.LOCAL_WRITE)) is True


def test_permission_policy_denies_ungranted_permission() -> None:
    policy = PermissionPolicy(granted=frozenset({Permission.READ}))
    assert policy.authorizes(_tool(Permission.EXTERNAL_WRITE)) is False
    assert policy.authorizes(_tool(Permission.CRITICAL)) is False


def test_permission_policy_with_no_grants_denies_everything() -> None:
    policy = PermissionPolicy(granted=frozenset())
    for permission in Permission:
        assert policy.authorizes(_tool(permission)) is False


@given(
    grants=st.frozensets(st.sampled_from(Permission)),
    permission=st.sampled_from(Permission),
)
@example(grants=frozenset(), permission=Permission.READ)
@example(grants=frozenset(Permission), permission=Permission.CRITICAL)
def test_permission_policy_authorizes_exactly_granted_permissions(
    grants: frozenset[Permission], permission: Permission
) -> None:
    policy = PermissionPolicy(granted=grants)
    assert policy.authorizes(_tool(permission)) == (permission in grants)


# --- ControlPolicy validation ----------------------------------------------


@pytest.mark.parametrize("no_progress_limit", [0, -1, -100])
def test_control_policy_rejects_non_positive_no_progress_limit(no_progress_limit: int) -> None:
    with pytest.raises(ValueError, match="no_progress_limit must be positive"):
        _policy(no_progress_limit=no_progress_limit)


# --- Stop rule: verified success -------------------------------------------


def test_verified_success_stops_run() -> None:
    decision = _policy().evaluate(_state(last_verification_passed=True), now=NOW)
    assert decision.kind is ControlDecisionKind.STOP_SUCCESS
    assert decision.reason_code == "STOP_SUCCESS_VERIFIED"
    assert decision.stop_reason is StopReason.SUCCESS_VERIFIED


def test_verified_success_takes_precedence_over_every_other_stop_rule() -> None:
    state = _state(
        last_verification_passed=True,
        cost_usd=100.0,
        input_tokens=100_000,
        iteration=100,
        consecutive_no_progress=100,
        started_at=NOW,
    )
    policy = _policy(max_total_tokens=1, max_elapsed_seconds=1.0)
    decision = policy.evaluate(state, now=datetime(2027, 1, 1, tzinfo=UTC))
    assert decision.kind is ControlDecisionKind.STOP_SUCCESS
    assert decision.stop_reason is StopReason.SUCCESS_VERIFIED


@pytest.mark.parametrize("passed", [False, None])
def test_unverified_run_does_not_stop_for_success(passed: bool | None) -> None:
    decision = _policy().evaluate(_state(last_verification_passed=passed), now=NOW)
    assert decision.kind is ControlDecisionKind.CONTINUE


# --- Stop rule: cost budget -------------------------------------------------


@pytest.mark.parametrize("cost_usd", [5.0, 5.01, 100.0])
def test_cost_at_or_above_budget_stops_run(cost_usd: float) -> None:
    decision = _policy().evaluate(_state(cost_usd=cost_usd), now=NOW)
    assert decision.kind is ControlDecisionKind.STOP_BUDGET
    assert decision.reason_code == "STOP_BUDGET_EXHAUSTED"
    assert decision.stop_reason is StopReason.BUDGET_EXHAUSTED


def test_cost_below_budget_does_not_stop_run() -> None:
    decision = _policy().evaluate(_state(cost_usd=4.99), now=NOW)
    assert decision.kind is ControlDecisionKind.CONTINUE


def test_cost_budget_takes_precedence_over_token_budget() -> None:
    policy = _policy(max_total_tokens=10)
    state = _state(cost_usd=5.0, input_tokens=10)
    decision = policy.evaluate(state, now=NOW)
    assert decision.reason_code == "STOP_BUDGET_EXHAUSTED"


# --- Stop rule: token budget ------------------------------------------------


@pytest.mark.parametrize("tokens", [100, 101, 100_000])
def test_tokens_at_or_above_cap_stop_run(tokens: int) -> None:
    policy = _policy(max_total_tokens=100)
    state = _state(input_tokens=tokens // 2, output_tokens=tokens - tokens // 2)
    decision = policy.evaluate(state, now=NOW)
    assert decision.kind is ControlDecisionKind.STOP_BUDGET
    assert decision.reason_code == "STOP_TOKEN_BUDGET_EXHAUSTED"
    assert decision.stop_reason is StopReason.BUDGET_EXHAUSTED


def test_tokens_below_cap_do_not_stop_run() -> None:
    policy = _policy(max_total_tokens=100)
    decision = policy.evaluate(_state(input_tokens=50, output_tokens=49), now=NOW)
    assert decision.kind is ControlDecisionKind.CONTINUE


def test_token_rule_is_skipped_when_no_token_cap_is_set() -> None:
    decision = _policy().evaluate(_state(input_tokens=10**12), now=NOW)
    assert decision.kind is ControlDecisionKind.CONTINUE


def test_token_budget_takes_precedence_over_elapsed_budget() -> None:
    policy = _policy(max_total_tokens=10, max_elapsed_seconds=60.0)
    state = _state(input_tokens=10, started_at=NOW)
    decision = policy.evaluate(state, now=datetime(2026, 8, 22, 1, 0, tzinfo=UTC))
    assert decision.reason_code == "STOP_TOKEN_BUDGET_EXHAUSTED"


# --- Stop rule: elapsed-time budget ------------------------------------------


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 22, 0, 1, tzinfo=UTC),  # exactly 60s elapsed
        datetime(2026, 8, 22, 1, 0, tzinfo=UTC),  # well past the cap
    ],
)
def test_elapsed_at_or_past_cap_stops_run(now: datetime) -> None:
    policy = _policy(max_elapsed_seconds=60.0)
    decision = policy.evaluate(_state(started_at=NOW), now=now)
    assert decision.kind is ControlDecisionKind.STOP_BUDGET
    assert decision.reason_code == "STOP_TIME_BUDGET_EXHAUSTED"
    assert decision.stop_reason is StopReason.BUDGET_EXHAUSTED


def test_elapsed_below_cap_does_not_stop_run() -> None:
    policy = _policy(max_elapsed_seconds=60.0)
    now = datetime(2026, 8, 22, 0, 0, 59, tzinfo=UTC)
    decision = policy.evaluate(_state(started_at=NOW), now=now)
    assert decision.kind is ControlDecisionKind.CONTINUE


def test_elapsed_rule_is_skipped_without_now() -> None:
    policy = _policy(max_elapsed_seconds=60.0)
    decision = policy.evaluate(_state(started_at=NOW), now=None)
    assert decision.kind is ControlDecisionKind.CONTINUE


def test_elapsed_rule_is_skipped_without_start_time() -> None:
    policy = _policy(max_elapsed_seconds=60.0)
    decision = policy.evaluate(_state(started_at=None), now=datetime(2030, 1, 1, tzinfo=UTC))
    assert decision.kind is ControlDecisionKind.CONTINUE


def test_elapsed_rule_is_skipped_when_no_elapsed_cap_is_set() -> None:
    state = _state(started_at=NOW)
    decision = _policy().evaluate(state, now=datetime(2030, 1, 1, tzinfo=UTC))
    assert decision.kind is ControlDecisionKind.CONTINUE


# --- Stop rule: stalled on no progress ---------------------------------------


@pytest.mark.parametrize("no_progress", [3, 4, 50])
def test_no_progress_at_or_above_limit_stops_run_stalled(no_progress: int) -> None:
    decision = _policy().evaluate(_state(consecutive_no_progress=no_progress), now=NOW)
    assert decision.kind is ControlDecisionKind.STOP_STALLED
    assert decision.reason_code == "STOP_STALLED_NO_PROGRESS"
    assert decision.stop_reason is StopReason.STALLED


def test_no_progress_below_limit_does_not_stop_run() -> None:
    decision = _policy().evaluate(_state(consecutive_no_progress=2), now=NOW)
    assert decision.kind is ControlDecisionKind.CONTINUE


def test_stalled_takes_precedence_over_max_iterations() -> None:
    policy = _policy(no_progress_limit=2)
    state = _state(consecutive_no_progress=2, iteration=10)
    decision = policy.evaluate(state, now=NOW)
    assert decision.kind is ControlDecisionKind.STOP_STALLED
    assert decision.stop_reason is StopReason.STALLED


# --- Stop rule: max iterations -----------------------------------------------


@pytest.mark.parametrize("iteration", [10, 11, 500])
def test_iteration_at_or_above_max_stops_run_failed(iteration: int) -> None:
    decision = _policy().evaluate(_state(iteration=iteration), now=NOW)
    assert decision.kind is ControlDecisionKind.STOP_FAILURE
    assert decision.reason_code == "STOP_MAX_ITERATIONS"
    assert decision.stop_reason is StopReason.MAX_ITERATIONS


def test_iteration_below_max_does_not_stop_run() -> None:
    decision = _policy().evaluate(_state(iteration=9), now=NOW)
    assert decision.kind is ControlDecisionKind.CONTINUE


# --- Continue decision --------------------------------------------------------


def test_run_within_all_limits_continues() -> None:
    state = _state(
        last_verification_passed=False,
        cost_usd=1.0,
        input_tokens=10,
        iteration=3,
        consecutive_no_progress=1,
        started_at=NOW,
    )
    policy = _policy(max_total_tokens=100, max_elapsed_seconds=3600.0)
    decision = policy.evaluate(state, now=NOW)
    assert decision.kind is ControlDecisionKind.CONTINUE
    assert decision.reason_code == "CONTINUE_WITHIN_POLICY"
    assert decision.stop_reason is None


# --- Stop precedence invariant ------------------------------------------------


def _expected_kind(
    state: RunState, policy: ControlPolicy, now: datetime | None
) -> ControlDecisionKind:
    """Independent transcription of the documented stop precedence chain."""
    budget = policy.budget
    kind = ControlDecisionKind.CONTINUE
    if state.last_verification_passed is True:
        kind = ControlDecisionKind.STOP_SUCCESS
    elif (
        state.cost_usd >= budget.max_cost_usd
        or (budget.max_total_tokens is not None and state.total_tokens >= budget.max_total_tokens)
        or (
            budget.max_elapsed_seconds is not None
            and state.started_at is not None
            and now is not None
            and (now - state.started_at).total_seconds() >= budget.max_elapsed_seconds
        )
    ):
        kind = ControlDecisionKind.STOP_BUDGET
    elif state.consecutive_no_progress >= policy.no_progress_limit:
        kind = ControlDecisionKind.STOP_STALLED
    elif state.iteration >= budget.max_iterations:
        kind = ControlDecisionKind.STOP_FAILURE
    return kind


_AWARE_DATETIMES = st.integers(min_value=946_684_800, max_value=4_102_444_800).map(
    lambda timestamp: datetime.fromtimestamp(timestamp, tz=UTC)
)


@given(
    state=st.builds(
        RunState,
        run_id=st.just(RUN),
        last_verification_passed=st.one_of(st.none(), st.booleans()),
        cost_usd=st.floats(min_value=0.0, max_value=60.0, allow_nan=False),
        input_tokens=st.integers(min_value=0, max_value=6000),
        output_tokens=st.integers(min_value=0, max_value=100),
        iteration=st.integers(min_value=0, max_value=25),
        consecutive_no_progress=st.integers(min_value=0, max_value=6),
        started_at=st.one_of(st.none(), _AWARE_DATETIMES),
    ),
    budget=st.builds(
        BudgetLimit,
        max_cost_usd=st.floats(min_value=0.5, max_value=50.0, allow_nan=False),
        max_iterations=st.integers(min_value=1, max_value=20),
        max_total_tokens=st.one_of(st.none(), st.integers(min_value=1, max_value=5000)),
        max_elapsed_seconds=st.one_of(
            st.none(), st.floats(min_value=0.5, max_value=10_000.0, allow_nan=False)
        ),
    ),
    now=st.one_of(st.none(), _AWARE_DATETIMES),
    no_progress_limit=st.integers(min_value=1, max_value=5),
)
@example(
    state=RunState(
        run_id=RUN,
        last_verification_passed=True,
        cost_usd=60.0,
        input_tokens=6000,
        output_tokens=100,
        iteration=25,
        consecutive_no_progress=6,
        started_at=datetime(2000, 1, 1, tzinfo=UTC),
    ),
    budget=BudgetLimit(
        max_cost_usd=0.5,
        max_iterations=1,
        max_total_tokens=1,
        max_elapsed_seconds=0.5,
    ),
    now=datetime(2100, 1, 1, tzinfo=UTC),
    no_progress_limit=1,
)
@example(
    state=RunState(run_id=RUN),
    budget=BudgetLimit(max_cost_usd=50.0, max_iterations=20),
    now=None,
    no_progress_limit=5,
)
def test_evaluate_matches_documented_stop_precedence(
    state: RunState,
    budget: BudgetLimit,
    now: datetime | None,
    no_progress_limit: int,
) -> None:
    policy = ControlPolicy(budget=budget, no_progress_limit=no_progress_limit)
    decision = policy.evaluate(state, now=now)
    assert decision.kind == _expected_kind(state, policy, now)
    if decision.kind is ControlDecisionKind.CONTINUE:
        assert decision.stop_reason is None
    else:
        assert decision.stop_reason is not None
