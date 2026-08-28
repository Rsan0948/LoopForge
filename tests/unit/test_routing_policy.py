"""Deterministic routing policy tests — fake adapters only (acceptance gate)."""

from __future__ import annotations

import pytest

from loopforge.adapters.model_registry import ModelRegistry, ModelRegistryEntry
from loopforge.adapters.routing import (
    TieredRoutingPolicy,
    _cheapest_at_or_above,  # pyright: ignore[reportPrivateUsage] - direct call pins the defensive branch
)
from loopforge.adapters.scripted import ScriptedModel
from loopforge.domain.actions import ActionProposal
from loopforge.domain.routing import (
    ModelCapabilities,
    ModelRequirements,
    ModelTier,
    RouteReason,
    RoutingPolicyConfig,
)
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunId
from loopforge.ports.routing import RoutingDecision, RoutingSignals


def _model(  # noqa: PLR0913 - capability knobs stay explicit at each call site
    provider: str,
    name: str,
    *,
    tool_calls: bool = True,
    window: int = 8192,
    input_cost: float = 0.0,
    output_cost: float = 0.0,
) -> ScriptedModel:
    return ScriptedModel(
        [ActionProposal(ActionId("a1"), "inspect", {})],
        capabilities=ModelCapabilities(
            provider=provider,
            model=name,
            supports_tool_calls=tool_calls,
            context_window_tokens=window,
            input_cost_usd_per_million=input_cost,
            output_cost_usd_per_million=output_cost,
        ),
    )


def _registry(
    *entries: tuple[ScriptedModel, ModelTier],
) -> ModelRegistry:
    return ModelRegistry(
        tuple(ModelRegistryEntry(model=model, tier=tier) for model, tier in entries)
    )


def _policy(
    registry: ModelRegistry,
    *,
    requirements: ModelRequirements | None = None,
    default_tier: ModelTier = ModelTier.STANDARD,
    stall_threshold: int = 2,
    budget_fraction: float | None = None,
) -> TieredRoutingPolicy:
    return TieredRoutingPolicy(
        registry,
        config=RoutingPolicyConfig(
            requirements=requirements or ModelRequirements(supports_tool_calls=True),
            default_tier=default_tier,
            stall_escalation_threshold=stall_threshold,
            budget_pressure_remaining_fraction=budget_fraction,
        ),
    )


def _state(*, no_progress: int = 0) -> RunState:
    return RunState(run_id=RunId("run_test"), consecutive_no_progress=no_progress)


# --- RoutingSignals / RoutingDecision invariants ------------------------------------


def test_routing_signals_validation() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        RoutingSignals(model_failure_streak=True)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="cannot be negative"):
        RoutingSignals(model_failure_streak=-1)
    with pytest.raises(ValueError, match="must be finite"):
        RoutingSignals(budget_remaining_fraction=float("nan"))
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        RoutingSignals(budget_remaining_fraction=1.5)


def test_routing_decision_invariants() -> None:
    model = _model("p", "m")
    with pytest.raises(ValueError, match="only ROUTE_NO_COMPATIBLE_MODEL"):
        RoutingDecision(reason_code=RouteReason.ROUTE_RETAINED_CURRENT, model=None, tier=None)
    with pytest.raises(ValueError, match="cannot carry a tier"):
        RoutingDecision(
            reason_code=RouteReason.ROUTE_NO_COMPATIBLE_MODEL,
            model=None,
            tier=ModelTier.ECONOMY,
        )
    with pytest.raises(ValueError, match="must carry its tier"):
        RoutingDecision(reason_code=RouteReason.ROUTE_RETAINED_CURRENT, model=model, tier=None)


def test_routing_decision_coerces_plain_string_codes_and_tiers() -> None:
    decision = RoutingDecision(
        reason_code="ROUTE_RETAINED_CURRENT",  # pyright: ignore[reportArgumentType]
        model=_model("p", "m"),
        tier="standard",  # pyright: ignore[reportArgumentType]
    )
    assert decision.reason_code is RouteReason.ROUTE_RETAINED_CURRENT
    assert decision.tier is ModelTier.STANDARD


# --- Initial selection and retention -------------------------------------------------


def test_initial_selection_picks_cheapest_model_at_default_tier() -> None:
    pricey = _model("p1", "standard-pricey", input_cost=5.0)
    cheap = _model("p2", "standard-cheap", input_cost=1.0)
    policy = _policy(
        _registry(
            (_model("p0", "economy"), ModelTier.ECONOMY),
            (pricey, ModelTier.STANDARD),
            (cheap, ModelTier.STANDARD),
            (_model("p3", "advanced"), ModelTier.ADVANCED),
        )
    )
    decision = policy.route(_state(), signals=RoutingSignals())
    assert decision.reason_code is RouteReason.ROUTE_INITIAL_SELECTION
    assert decision.model is cheap
    assert decision.tier is ModelTier.STANDARD


def test_initial_selection_climbs_to_next_available_tier() -> None:
    advanced = _model("p", "advanced")
    policy = _policy(
        _registry(
            (_model("p", "economy"), ModelTier.ECONOMY),
            (advanced, ModelTier.ADVANCED),
        )
    )
    decision = policy.route(_state(), signals=RoutingSignals())
    assert decision.reason_code is RouteReason.ROUTE_INITIAL_SELECTION
    assert decision.model is advanced
    assert decision.tier is ModelTier.ADVANCED


def test_current_model_is_retained_when_nothing_demands_change() -> None:
    current = _model("p", "standard")
    policy = _policy(_registry((current, ModelTier.STANDARD)))
    decision = policy.route(_state(), signals=RoutingSignals(current_model=current.capabilities))
    assert decision.reason_code is RouteReason.ROUTE_RETAINED_CURRENT
    assert decision.model is current


def test_incompatible_current_model_is_never_retained() -> None:
    incompatible = _model("p", "no-tools", tool_calls=False)
    fit = _model("p", "fit")
    policy = _policy(
        _registry(
            (incompatible, ModelTier.STANDARD),
            (fit, ModelTier.STANDARD),
        )
    )
    decision = policy.route(
        _state(), signals=RoutingSignals(current_model=incompatible.capabilities)
    )
    assert decision.model is fit
    assert decision.reason_code is RouteReason.ROUTE_INITIAL_SELECTION


def test_no_compatible_model_fails_closed_with_reason_code() -> None:
    policy = _policy(_registry((_model("p", "no-tools", tool_calls=False), ModelTier.STANDARD)))
    decision = policy.route(_state(), signals=RoutingSignals())
    assert decision.reason_code is RouteReason.ROUTE_NO_COMPATIBLE_MODEL
    assert decision.model is None
    assert decision.tier is None


# --- Vertical escalation on stalls ----------------------------------------------------


def test_stall_escalates_to_next_stronger_tier() -> None:
    current = _model("p", "standard")
    advanced = _model("p", "advanced")
    policy = _policy(_registry((current, ModelTier.STANDARD), (advanced, ModelTier.ADVANCED)))
    decision = policy.route(
        _state(no_progress=2), signals=RoutingSignals(current_model=current.capabilities)
    )
    assert decision.reason_code is RouteReason.ESCALATED_STALL
    assert decision.model is advanced
    assert decision.tier is ModelTier.ADVANCED


def test_stall_below_threshold_retains_current_model() -> None:
    current = _model("p", "standard")
    policy = _policy(
        _registry(
            (current, ModelTier.STANDARD),
            (_model("p", "advanced"), ModelTier.ADVANCED),
        )
    )
    decision = policy.route(
        _state(no_progress=1), signals=RoutingSignals(current_model=current.capabilities)
    )
    assert decision.reason_code is RouteReason.ROUTE_RETAINED_CURRENT
    assert decision.model is current


def test_stall_without_stronger_candidate_retains_honestly() -> None:
    current = _model("p", "advanced")
    policy = _policy(_registry((current, ModelTier.ADVANCED)))
    decision = policy.route(
        _state(no_progress=5), signals=RoutingSignals(current_model=current.capabilities)
    )
    assert decision.reason_code is RouteReason.ROUTE_RETAINED_CURRENT
    assert decision.model is current


# --- Budget pressure de-escalation -----------------------------------------------------


def test_budget_pressure_deescalates_to_cheapest_compatible_tier() -> None:
    economy = _model("p", "economy")
    current = _model("p", "advanced")
    policy = _policy(
        _registry(
            (economy, ModelTier.ECONOMY),
            (_model("p", "standard"), ModelTier.STANDARD),
            (current, ModelTier.ADVANCED),
        ),
        budget_fraction=0.25,
    )
    decision = policy.route(
        _state(),
        signals=RoutingSignals(current_model=current.capabilities, budget_remaining_fraction=0.2),
    )
    assert decision.reason_code is RouteReason.DEESCALATED_BUDGET_PRESSURE
    assert decision.model is economy
    assert decision.tier is ModelTier.ECONOMY


def test_budget_pressure_takes_precedence_over_stall_escalation() -> None:
    economy = _model("p", "economy")
    current = _model("p", "standard")
    policy = _policy(
        _registry(
            (economy, ModelTier.ECONOMY),
            (current, ModelTier.STANDARD),
            (_model("p", "advanced"), ModelTier.ADVANCED),
        ),
        budget_fraction=0.25,
    )
    decision = policy.route(
        _state(no_progress=9),
        signals=RoutingSignals(current_model=current.capabilities, budget_remaining_fraction=0.1),
    )
    assert decision.reason_code is RouteReason.DEESCALATED_BUDGET_PRESSURE
    assert decision.model is economy


def test_budget_above_pressure_threshold_retains_current_model() -> None:
    current = _model("p", "standard")
    policy = _policy(
        _registry(
            (_model("p", "economy"), ModelTier.ECONOMY),
            (current, ModelTier.STANDARD),
        ),
        budget_fraction=0.25,
    )
    decision = policy.route(
        _state(),
        signals=RoutingSignals(current_model=current.capabilities, budget_remaining_fraction=0.8),
    )
    assert decision.reason_code is RouteReason.ROUTE_RETAINED_CURRENT
    assert decision.model is current


# --- Horizontal provider fallback -------------------------------------------------------


def test_transient_failure_falls_back_to_other_provider_at_equal_tier() -> None:
    failing = _model("provider-a", "standard")
    alternative = _model("provider-b", "standard")
    policy = _policy(
        _registry(
            (failing, ModelTier.STANDARD),
            (alternative, ModelTier.STANDARD),
        )
    )
    decision = policy.route(
        _state(),
        signals=RoutingSignals(
            current_model=failing.capabilities,
            model_failure_streak=1,
            request_fallback=True,
        ),
    )
    assert decision.reason_code is RouteReason.FALLBACK_TRANSIENT_FAILURE
    assert decision.model is alternative


def test_fallback_prefers_equal_or_stronger_tier_never_weaker() -> None:
    failing = _model("provider-a", "advanced")
    weaker = _model("provider-b", "economy")
    policy = _policy(
        _registry(
            (failing, ModelTier.ADVANCED),
            (weaker, ModelTier.ECONOMY),
        )
    )
    decision = policy.route(
        _state(),
        signals=RoutingSignals(current_model=failing.capabilities, request_fallback=True),
    )
    # The only alternative is a weaker tier: fallback is refused honestly and
    # the current model is retained for the bounded retry.
    assert decision.reason_code is RouteReason.FALLBACK_UNAVAILABLE
    assert decision.model is failing


def test_fallback_without_alternative_retains_current_model() -> None:
    failing = _model("provider-a", "standard")
    policy = _policy(_registry((failing, ModelTier.STANDARD)))
    decision = policy.route(
        _state(),
        signals=RoutingSignals(current_model=failing.capabilities, request_fallback=True),
    )
    assert decision.reason_code is RouteReason.FALLBACK_UNAVAILABLE
    assert decision.model is failing


def test_fallback_ignores_incompatible_alternatives() -> None:
    failing = _model("provider-a", "standard")
    incompatible = _model("provider-b", "no-tools", tool_calls=False)
    policy = _policy(
        _registry(
            (failing, ModelTier.STANDARD),
            (incompatible, ModelTier.STANDARD),
        )
    )
    decision = policy.route(
        _state(),
        signals=RoutingSignals(current_model=failing.capabilities, request_fallback=True),
    )
    assert decision.reason_code is RouteReason.FALLBACK_UNAVAILABLE
    assert decision.model is failing


# --- Determinism ------------------------------------------------------------------------


def test_selection_is_deterministic_for_identical_costs() -> None:
    first = _model("provider-b", "beta")
    second = _model("provider-a", "alpha")
    policy = _policy(
        _registry(
            (first, ModelTier.STANDARD),
            (second, ModelTier.STANDARD),
        )
    )
    decision = policy.route(_state(), signals=RoutingSignals())
    # Equal cost: provider/model name ordering makes the choice total.
    assert decision.model is second
    repeat = policy.route(_state(), signals=RoutingSignals())
    assert repeat.model is decision.model


def test_budget_pressure_at_cheapest_tier_already_retains_current_model() -> None:
    current = _model("p", "economy")
    policy = _policy(
        _registry(
            (current, ModelTier.ECONOMY),
            (_model("p", "advanced"), ModelTier.ADVANCED),
        ),
        budget_fraction=0.5,
    )
    decision = policy.route(
        _state(),
        signals=RoutingSignals(current_model=current.capabilities, budget_remaining_fraction=0.1),
    )
    # Pressure is real but nothing cheaper exists: retention is the honest answer.
    assert decision.reason_code is RouteReason.ROUTE_RETAINED_CURRENT
    assert decision.model is current


def test_tier_selection_requires_a_candidate_at_or_above_target() -> None:
    entry = ModelRegistryEntry(model=_model("p", "economy"), tier=ModelTier.ECONOMY)
    with pytest.raises(RuntimeError, match="at or above the target tier"):
        _cheapest_at_or_above((entry,), 99)
