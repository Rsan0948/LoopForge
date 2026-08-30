"""Unit pins for the orchestrator/worker domain vocabulary (PACS-013)."""

from __future__ import annotations

import pytest

from loopforge.domain.orchestration import (
    MAX_WORKER_ID_CHARS,
    MAX_WORKER_TEXT_CHARS,
    MergeOutcome,
    WorkerOutcome,
    WorkerProjection,
    WorkerSpec,
    partition_budget,
    validate_budget_share,
    validate_worker_id,
    validate_worker_text,
)
from loopforge.domain.types import BudgetLimit, RunId, WorkerId, WorkspaceId


def test_worker_outcome_vocabulary_is_closed() -> None:
    assert {outcome.value for outcome in WorkerOutcome} == {
        "succeeded",
        "failed",
        "budget_exhausted",
        "cancelled",
    }


def test_merge_outcome_vocabulary_is_closed() -> None:
    assert {outcome.value for outcome in MergeOutcome} == {"merged", "conflict"}


def test_validate_worker_id_accepts_plain_name() -> None:
    validate_worker_id("worker-adder_01")


@pytest.mark.parametrize("worker_id", ["", "   ", "a/b", "a\\b", ".", ".."])
def test_validate_worker_id_rejects_empty_and_separator_names(worker_id: str) -> None:
    with pytest.raises(ValueError, match="worker_id"):
        validate_worker_id(worker_id)


@pytest.mark.parametrize("worker_id", ["a\x00b", "a\x1fb", "a\x7fb"])
def test_validate_worker_id_rejects_control_characters(worker_id: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        validate_worker_id(worker_id)


def test_validate_worker_id_rejects_overlong_name() -> None:
    with pytest.raises(ValueError, match="128 characters"):
        validate_worker_id("w" * (MAX_WORKER_ID_CHARS + 1))


def test_validate_worker_text_accepts_bounded_text() -> None:
    validate_worker_text("repair the adder module", "worker objective")


@pytest.mark.parametrize("text", ["", "   "])
def test_validate_worker_text_rejects_empty(text: str) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_worker_text(text, "worker objective")


def test_validate_worker_text_rejects_control_characters() -> None:
    with pytest.raises(ValueError, match="control characters"):
        validate_worker_text("bad\x00text", "worker objective")


def test_validate_worker_text_rejects_overlong_text() -> None:
    with pytest.raises(ValueError, match=str(MAX_WORKER_TEXT_CHARS)):
        validate_worker_text("t" * (MAX_WORKER_TEXT_CHARS + 1), "worker objective")


def test_validate_budget_share_accepts_positive_finite() -> None:
    validate_budget_share(0.5)


@pytest.mark.parametrize("share", [0.0, -0.1, float("inf"), float("-inf"), float("nan")])
def test_validate_budget_share_rejects_non_positive_or_non_finite(share: float) -> None:
    with pytest.raises(ValueError, match="budget share"):
        validate_budget_share(share)


def test_worker_spec_validates_assignment_fields() -> None:
    spec = WorkerSpec(
        worker_id=WorkerId("adder"),
        workspace_id=WorkspaceId("calculator-adder"),
        objective="repair adder.py",
        budget=BudgetLimit(max_cost_usd=1.0, max_iterations=4),
    )
    assert spec.worker_id == WorkerId("adder")
    with pytest.raises(ValueError, match="worker_id"):
        WorkerSpec(
            worker_id=WorkerId("a/b"),
            workspace_id=WorkspaceId("calculator-adder"),
            objective="repair adder.py",
            budget=BudgetLimit(max_cost_usd=1.0, max_iterations=1),
        )
    with pytest.raises(ValueError, match="cannot be empty"):
        WorkerSpec(
            worker_id=WorkerId("adder"),
            workspace_id=WorkspaceId("calculator-adder"),
            objective="   ",
            budget=BudgetLimit(max_cost_usd=1.0, max_iterations=1),
        )
    with pytest.raises(TypeError, match="BudgetLimit"):
        WorkerSpec(
            worker_id=WorkerId("adder"),
            workspace_id=WorkspaceId("calculator-adder"),
            objective="repair adder.py",
            budget=1.0,  # type: ignore[arg-type]
        )


def test_worker_projection_defaults_to_unresolved() -> None:
    projection = WorkerProjection(
        worker_id=WorkerId("adder"),
        worker_run_id=RunId("run_worker"),
        workspace_id=WorkspaceId("calculator-adder"),
        budget_share_cost_usd=1.0,
    )
    assert projection.outcome is None
    assert projection.merge_outcome is None


def test_partition_budget_splits_cost_into_equal_static_shares() -> None:
    limit = BudgetLimit(max_cost_usd=2.0, max_iterations=8, max_elapsed_seconds=60.0)
    shares = partition_budget(limit, 2)
    assert len(shares) == 2
    # 2.0 / 2 is exact in binary floating point.
    assert all(share.max_cost_usd == 1.0 for share in shares)
    # Iteration and elapsed ceilings are per-stream concerns: inherited whole.
    assert all(share.max_iterations == 8 for share in shares)
    assert all(share.max_elapsed_seconds == 60.0 for share in shares)


def test_partition_budget_shares_never_sum_above_global_limit() -> None:
    limit = BudgetLimit(max_cost_usd=0.1, max_iterations=1)
    shares = partition_budget(limit, 3)
    assert sum(share.max_cost_usd for share in shares) <= limit.max_cost_usd
    assert all(share.max_cost_usd > 0 for share in shares)


def test_partition_budget_floors_token_shares() -> None:
    limit = BudgetLimit(max_cost_usd=1.0, max_iterations=1, max_total_tokens=101)
    shares = partition_budget(limit, 2)
    assert all(share.max_total_tokens == 50 for share in shares)
    assert sum(share.max_total_tokens or 0 for share in shares) <= 101


def test_partition_budget_single_worker_inherits_whole_limit() -> None:
    limit = BudgetLimit(max_cost_usd=2.0, max_iterations=8, max_total_tokens=1000)
    (share,) = partition_budget(limit, 1)
    assert share == limit


@pytest.mark.parametrize("count", [0, -1])
def test_partition_budget_requires_at_least_one_worker(count: int) -> None:
    with pytest.raises(ValueError, match="at least one"):
        partition_budget(BudgetLimit(max_cost_usd=1.0, max_iterations=1), count)


@pytest.mark.parametrize("count", [True, 2.0, "2"])
def test_partition_budget_requires_integer_count(count: object) -> None:
    with pytest.raises(ValueError, match="integer"):
        partition_budget(BudgetLimit(max_cost_usd=1.0, max_iterations=1), count)  # type: ignore[arg-type]


def test_partition_budget_rejects_non_budget_limit() -> None:
    with pytest.raises(TypeError, match="BudgetLimit"):
        partition_budget(1.0, 2)  # type: ignore[arg-type]


def test_partition_budget_rejects_count_too_large_for_cost_budget() -> None:
    # The smallest positive double underflows to zero when halved.
    tiny = BudgetLimit(max_cost_usd=5e-324, max_iterations=1)
    with pytest.raises(ValueError, match="too large for the global cost budget"):
        partition_budget(tiny, 2)


def test_partition_budget_rejects_count_too_large_for_token_budget() -> None:
    limit = BudgetLimit(max_cost_usd=10.0, max_iterations=1, max_total_tokens=3)
    with pytest.raises(ValueError, match="too large for the global token budget"):
        partition_budget(limit, 4)
