"""Adaptive context-budget allocator tests (PACS-017 M2).

Allow+deny pairs per AGENTS.md rule 10: the allocator adapts the per-turn
budget only inside policy bounds, construction fails closed when bounds
would widen the wired envelope, and the delegate's enforcement contracts
(preservation, explicit budget failure) are pinned unharmed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from loopforge.adapters.context import (
    AdaptiveContextBuilder,
    BasicContextBuilder,
    BudgetedContextBuilder,
    CharsPerTokenCounter,
)
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import FixedClock, RecordingSleeper
from loopforge.domain.context import ModelRole
from loopforge.domain.context_lifecycle import (
    ContextBudgetError,
    ContextTokenBudget,
    DropReason,
)
from loopforge.domain.policies import ContextAllocationBounds
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.state import RunState
from loopforge.domain.types import RunId, RunStatus
from loopforge.entrypoints.repair import RepairRuntimeDeps, repair_context_builder
from loopforge.workloads.repair import RepairContextBuilder

NOW = datetime(2026, 9, 5, tzinfo=UTC)
RUN = RunId("run-adaptive")
ENVELOPE = ContextTokenBudget(max_tokens=4096, reserve_tokens=0)
BIG = "x" * 24000  # ~6000 tokens at 4 chars/token — un-fittable at any test budget


def _bounds(**overrides: object) -> ContextAllocationBounds:
    base: dict[str, object] = {"floor_tokens": 1024, "ceiling_tokens": 4096, "step_tokens": 512}
    base.update(overrides)
    return ContextAllocationBounds(**base)  # pyright: ignore[reportArgumentType]


def _adaptive(
    bounds: ContextAllocationBounds,
    *,
    envelope: ContextTokenBudget = ENVELOPE,
    basic: bool = False,
) -> AdaptiveContextBuilder:
    if basic:
        return AdaptiveContextBuilder(
            BasicContextBuilder(FixedClock(NOW)), bounds=bounds, envelope=envelope
        )
    return AdaptiveContextBuilder(
        BudgetedContextBuilder(
            FixedClock(NOW),
            CharsPerTokenCounter(),
            template=default_controller_template(),
            token_budget=ENVELOPE,
        ),
        bounds=bounds,
        envelope=envelope,
    )


def _big_state() -> RunState:
    return RunState(
        run_id=RUN,
        status=RunStatus.READY,
        objective="repair the widget",
        plan=BIG,
        last_observation=BIG,
        last_reflection=BIG,
    )


def _small_state() -> RunState:
    return RunState(run_id=RUN, status=RunStatus.READY, objective="repair the widget")


def _dropped_over_budget(builder: AdaptiveContextBuilder) -> bool:
    accounting = builder.last_accounting
    assert accounting is not None
    return any(entry.drop_reason is DropReason.OVER_BUDGET for entry in accounting.dropped_entries)


# --- Construction: allow + deny -----------------------------------------------


def test_construction_allows_bounds_inside_the_envelope() -> None:
    builder = _adaptive(_bounds())
    assert builder.current_budget == ContextTokenBudget(max_tokens=1024, reserve_tokens=0)


def test_construction_allows_ceiling_equal_to_the_envelope() -> None:
    _adaptive(_bounds(ceiling_tokens=4096), envelope=ContextTokenBudget(max_tokens=4096))


def test_construction_denies_ceiling_above_the_envelope() -> None:
    with pytest.raises(ValueError, match="ceiling cannot exceed"):
        _adaptive(_bounds(ceiling_tokens=8192))


def test_construction_denies_reserve_undercutting_the_envelope() -> None:
    with pytest.raises(ValueError, match="reserve cannot undercut"):
        _adaptive(
            _bounds(reserve_tokens=128),
            envelope=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
        )


# --- Adaptation behavior --------------------------------------------------------


def test_first_build_requests_the_policy_floor() -> None:
    builder = _adaptive(_bounds())
    builder.build_context(_small_state())
    accounting = builder.last_accounting
    assert accounting is not None
    assert accounting.budget.max_tokens == 1024


def test_over_budget_drops_grow_the_next_budget_by_the_policy_step() -> None:
    builder = _adaptive(_bounds())
    builder.build_context(_big_state())
    assert _dropped_over_budget(builder)
    assert builder.current_budget.max_tokens == 1536


def test_low_utilization_shrinks_the_next_budget() -> None:
    builder = _adaptive(_bounds(low_utilization_fraction=0.9))
    builder.build_context(_big_state())  # grow to 1536
    assert builder.current_budget.max_tokens == 1536
    builder.build_context(_small_state())  # far below the 0.9 threshold
    assert builder.current_budget.max_tokens == 1024


def test_budget_never_leaves_the_policy_bounds() -> None:
    builder = _adaptive(_bounds())
    for _ in range(20):
        builder.build_context(_big_state())
    assert builder.current_budget.max_tokens == 4096  # clamped at the ceiling
    for _ in range(20):
        builder.build_context(_small_state())
    assert builder.current_budget.max_tokens == 1024  # clamped at the floor


def test_every_build_requests_a_budget_inside_the_bounds() -> None:
    builder = _adaptive(_bounds(low_utilization_fraction=0.9))
    for index in range(10):
        builder.build_context(_big_state() if index % 2 == 0 else _small_state())
        accounting = builder.last_accounting
        assert accounting is not None
        assert 1024 <= accounting.budget.max_tokens <= 4096


def test_fixed_bounds_keep_the_budget_constant() -> None:
    builder = _adaptive(_bounds(floor_tokens=2048, ceiling_tokens=2048))
    builder.build_context(_big_state())
    assert builder.current_budget.max_tokens == 2048
    builder.build_context(_small_state())
    assert builder.current_budget.max_tokens == 2048


def test_explicit_token_budget_bypasses_adaptation() -> None:
    builder = _adaptive(_bounds())
    builder.build_context(_big_state())  # grow to 1536
    explicit = ContextTokenBudget(max_tokens=2048)
    builder.build_context(_small_state(), token_budget=explicit)
    accounting = builder.last_accounting
    assert accounting is not None
    assert accounting.budget == explicit
    assert builder.current_budget.max_tokens == 1536  # untouched by the bypass


def test_role_is_forwarded_to_the_delegate() -> None:
    builder = _adaptive(_bounds())
    context = builder.build_context(_small_state(), role=ModelRole.REFLECTOR)
    assert context.role is ModelRole.REFLECTOR


def test_delegate_without_accounting_seam_stays_at_the_floor() -> None:
    builder = _adaptive(_bounds(), basic=True)
    builder.build_context(_big_state())
    builder.build_context(_small_state())
    assert builder.current_budget.max_tokens == 1024


def test_last_accounting_is_the_delegate_ledger() -> None:
    builder = _adaptive(_bounds())
    builder.build_context(_small_state())
    accounting = builder.last_accounting
    assert accounting is not None
    assert accounting.budget.max_tokens == 1024


# --- Delegate contracts pinned unharmed ----------------------------------------


def test_preservation_failure_still_fails_explicitly() -> None:
    builder = _adaptive(_bounds(floor_tokens=64, ceiling_tokens=64))
    state = RunState(run_id=RUN, status=RunStatus.READY, objective=BIG)
    with pytest.raises(ContextBudgetError):
        builder.build_context(state)


def test_failed_build_does_not_adapt() -> None:
    builder = _adaptive(_bounds(floor_tokens=64, ceiling_tokens=1024))
    state = RunState(run_id=RUN, status=RunStatus.READY, objective=BIG)
    with pytest.raises(ContextBudgetError):
        builder.build_context(state)
    assert builder.current_budget.max_tokens == 64  # unchanged


# --- Entrypoint wiring -----------------------------------------------------------


def _deps(**overrides: object) -> RepairRuntimeDeps:
    base: dict[str, object] = {
        "store": InMemoryEventStore(),
        "clock": FixedClock(NOW),
        "sleeper": RecordingSleeper(),
    }
    base.update(overrides)
    return RepairRuntimeDeps(**base)  # pyright: ignore[reportArgumentType]


def test_repair_wiring_stays_fixed_without_allocation() -> None:
    builder = repair_context_builder(_deps())
    assert isinstance(builder, RepairContextBuilder)
    inner = builder._delegate  # pyright: ignore[reportPrivateUsage] - wiring shape pin
    assert isinstance(inner, BudgetedContextBuilder)


def test_repair_wiring_wraps_adaptive_when_allocation_is_supplied() -> None:
    builder = repair_context_builder(
        _deps(
            context_allocation=ContextAllocationBounds(
                floor_tokens=1024, ceiling_tokens=4096, reserve_tokens=256
            )
        )
    )
    assert isinstance(builder, RepairContextBuilder)
    inner = builder._delegate  # pyright: ignore[reportPrivateUsage] - wiring shape pin
    assert isinstance(inner, AdaptiveContextBuilder)
    assert inner.current_budget == ContextTokenBudget(max_tokens=1024, reserve_tokens=256)


def test_repair_wiring_fails_closed_when_allocation_widens_the_envelope() -> None:
    with pytest.raises(ValueError, match="ceiling cannot exceed"):
        repair_context_builder(
            _deps(
                context_allocation=ContextAllocationBounds(
                    floor_tokens=1024, ceiling_tokens=8192, reserve_tokens=256
                )
            )
        )
