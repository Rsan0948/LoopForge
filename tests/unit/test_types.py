from __future__ import annotations

import dataclasses

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from loopforge.domain.types import (
    ActionId,
    BudgetLimit,
    ControlDecisionKind,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    RunStatus,
    StopReason,
    UsageDelta,
    WorkerId,
)

TERMINAL_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.STALLED,
    RunStatus.BUDGET_EXHAUSTED,
    RunStatus.CANCELLED,
}
ACTIVE_STATUSES = set(RunStatus) - TERMINAL_STATUSES


def test_new_type_ids_behave_as_plain_strings() -> None:
    assert RunId("run-1") == "run-1"
    assert EventId("event-1") == "event-1"
    assert ActionId("action-1") == "action-1"
    assert WorkerId("worker-1") == "worker-1"


def test_run_status_values_round_trip_through_strings() -> None:
    assert RunStatus("created") is RunStatus.CREATED
    assert RunStatus("waiting_for_approval") is RunStatus.WAITING_FOR_APPROVAL
    assert str(RunStatus.SUCCEEDED) == "succeeded"


def test_run_status_terminal_partition_is_exact() -> None:
    terminal = {status for status in RunStatus if status.is_terminal}
    assert terminal == TERMINAL_STATUSES
    for status in ACTIVE_STATUSES:
        assert not status.is_terminal


@given(status=st.sampled_from(RunStatus))
@example(status=RunStatus.SUCCEEDED)
@example(status=RunStatus.CREATED)
def test_run_status_is_terminal_matches_partition(status: RunStatus) -> None:
    assert status.is_terminal == (status in TERMINAL_STATUSES)


def test_terminal_run_statuses_are_a_superset_of_stop_reason_kinds() -> None:
    terminal_by_stop_reason = {
        StopReason.SUCCESS_VERIFIED: RunStatus.SUCCEEDED,
        StopReason.FAILURE: RunStatus.FAILED,
        StopReason.STALLED: RunStatus.STALLED,
        StopReason.BUDGET_EXHAUSTED: RunStatus.BUDGET_EXHAUSTED,
        StopReason.CANCELLED: RunStatus.CANCELLED,
    }
    for reason, status in terminal_by_stop_reason.items():
        assert status.is_terminal, reason.value


def test_remaining_enums_round_trip_through_strings() -> None:
    assert RiskLevel("read_only") is RiskLevel.READ_ONLY
    assert Permission("critical") is Permission.CRITICAL
    assert ControlDecisionKind("request_human") is ControlDecisionKind.REQUEST_HUMAN
    assert StopReason("max_iterations") is StopReason.MAX_ITERATIONS


def test_enum_values_are_unique_within_each_enum() -> None:
    for enum in (RunStatus, StopReason, RiskLevel, Permission, ControlDecisionKind):
        values = [member.value for member in enum]
        assert len(values) == len(set(values))


def test_budget_limit_accepts_minimal_limits() -> None:
    limit = BudgetLimit(max_cost_usd=1.0, max_iterations=10)
    assert limit.max_cost_usd == 1.0
    assert limit.max_iterations == 10
    assert limit.max_total_tokens is None
    assert limit.max_elapsed_seconds is None


def test_budget_limit_accepts_optional_token_and_elapsed_caps() -> None:
    limit = BudgetLimit(
        max_cost_usd=1.0,
        max_iterations=10,
        max_total_tokens=1000,
        max_elapsed_seconds=60.0,
    )
    assert limit.max_total_tokens == 1000
    assert limit.max_elapsed_seconds == 60.0


@pytest.mark.parametrize("max_cost_usd", [0.0, -0.01, -100.0])
def test_budget_limit_rejects_non_positive_cost(max_cost_usd: float) -> None:
    with pytest.raises(ValueError, match="max_cost_usd must be positive"):
        BudgetLimit(max_cost_usd=max_cost_usd, max_iterations=10)


@pytest.mark.parametrize("max_iterations", [0, -1, -100])
def test_budget_limit_rejects_non_positive_iterations(max_iterations: int) -> None:
    with pytest.raises(ValueError, match="max_iterations must be positive"):
        BudgetLimit(max_cost_usd=1.0, max_iterations=max_iterations)


@pytest.mark.parametrize("max_total_tokens", [0, -1])
def test_budget_limit_rejects_non_positive_token_cap(max_total_tokens: int) -> None:
    with pytest.raises(ValueError, match="max_total_tokens must be positive when set"):
        BudgetLimit(
            max_cost_usd=1.0,
            max_iterations=10,
            max_total_tokens=max_total_tokens,
        )


@pytest.mark.parametrize("max_elapsed_seconds", [0.0, -0.5])
def test_budget_limit_rejects_non_positive_elapsed_cap(max_elapsed_seconds: float) -> None:
    with pytest.raises(ValueError, match="max_elapsed_seconds must be positive when set"):
        BudgetLimit(
            max_cost_usd=1.0,
            max_iterations=10,
            max_elapsed_seconds=max_elapsed_seconds,
        )


@given(
    max_cost_usd=st.floats(min_value=1e-9, max_value=1e9, allow_nan=False),
    max_iterations=st.integers(min_value=1, max_value=1_000_000),
    max_total_tokens=st.one_of(st.none(), st.integers(min_value=1, max_value=1_000_000)),
    max_elapsed_seconds=st.one_of(
        st.none(), st.floats(min_value=1e-9, max_value=1e9, allow_nan=False)
    ),
)
@example(max_cost_usd=1.0, max_iterations=1, max_total_tokens=None, max_elapsed_seconds=None)
def test_budget_limit_accepts_any_positive_limits(
    max_cost_usd: float,
    max_iterations: int,
    max_total_tokens: int | None,
    max_elapsed_seconds: float | None,
) -> None:
    limit = BudgetLimit(
        max_cost_usd=max_cost_usd,
        max_iterations=max_iterations,
        max_total_tokens=max_total_tokens,
        max_elapsed_seconds=max_elapsed_seconds,
    )
    assert limit.max_cost_usd == max_cost_usd
    assert limit.max_iterations == max_iterations
    assert limit.max_total_tokens == max_total_tokens
    assert limit.max_elapsed_seconds == max_elapsed_seconds


def test_budget_limit_is_frozen_and_hashable() -> None:
    limit = BudgetLimit(max_cost_usd=1.0, max_iterations=10)
    with pytest.raises(dataclasses.FrozenInstanceError):
        limit.max_cost_usd = 2.0  # type: ignore[misc]
    assert len({limit, BudgetLimit(max_cost_usd=1.0, max_iterations=10)}) == 1


def test_usage_delta_defaults_to_zero() -> None:
    usage = UsageDelta()
    assert usage.cost_usd == 0.0
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cached_input_tokens == 0


def test_usage_delta_accepts_positive_amounts() -> None:
    usage = UsageDelta(
        cost_usd=0.25,
        input_tokens=100,
        output_tokens=50,
        cached_input_tokens=25,
    )
    assert usage.cost_usd == 0.25
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cached_input_tokens == 25


@pytest.mark.parametrize("cost_usd", [-0.01, -100.0])
def test_usage_delta_rejects_negative_cost(cost_usd: float) -> None:
    with pytest.raises(ValueError, match="cost_usd cannot be negative"):
        UsageDelta(cost_usd=cost_usd)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", -1),
        ("output_tokens", -1),
        ("cached_input_tokens", -1),
    ],
)
def test_usage_delta_rejects_negative_token_counts(field: str, value: int) -> None:
    with pytest.raises(ValueError, match="token counts cannot be negative"):
        UsageDelta(**{field: value})


@given(
    cost_usd=st.floats(min_value=0.0, max_value=1e9, allow_nan=False),
    input_tokens=st.integers(min_value=0, max_value=10_000_000),
    output_tokens=st.integers(min_value=0, max_value=10_000_000),
    cached_input_tokens=st.integers(min_value=0, max_value=10_000_000),
)
@example(cost_usd=0.0, input_tokens=0, output_tokens=0, cached_input_tokens=0)
def test_usage_delta_accepts_any_non_negative_amounts(
    cost_usd: float,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
) -> None:
    usage = UsageDelta(
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
    )
    assert usage.cost_usd >= 0.0
    assert min(usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) >= 0


def test_usage_delta_is_frozen_and_hashable() -> None:
    usage = UsageDelta(cost_usd=0.1, input_tokens=10)
    with pytest.raises(dataclasses.FrozenInstanceError):
        usage.cost_usd = 1.0  # type: ignore[misc]
    assert len({usage, UsageDelta(cost_usd=0.1, input_tokens=10)}) == 1
