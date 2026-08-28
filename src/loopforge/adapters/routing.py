"""Deterministic tiered routing policy over a model registry.

Vertical routing moves between strength/cost tiers on execution-state
signals; horizontal fallback moves across providers on transient failure.
Every decision is reason-coded, selection within a tier is fully
deterministic (cheapest, then provider/model name), and the policy holds no
authority — hard budgets, permissions, and stopping remain with
``ControlPolicy`` (AGENTS.md rule 12).
"""

from __future__ import annotations

from loopforge.adapters.model_registry import ModelRegistry, ModelRegistryEntry
from loopforge.domain.routing import TIER_RANK, RouteReason, RoutingPolicyConfig
from loopforge.domain.state import RunState
from loopforge.ports.routing import RoutingDecision, RoutingSignals


def _cheapest_first(entry: ModelRegistryEntry) -> tuple[float, str, str]:
    """Total deterministic ordering within a tier: cost, then identity."""
    capabilities = entry.capabilities
    return (
        capabilities.input_cost_usd_per_million + capabilities.output_cost_usd_per_million,
        capabilities.provider,
        capabilities.model,
    )


def _cheapest_at_or_above(
    candidates: tuple[ModelRegistryEntry, ...], target_rank: int
) -> ModelRegistryEntry:
    """Cheapest entry at the target tier, climbing only when that tier is empty.

    Tier semantics stay meaningful: a stronger tier is chosen over a weaker
    one only when the target tier has no candidate, never merely because it
    is cheaper — vertical movement is an explicit escalation decision, not a
    side effect of pricing.
    """
    ranks = sorted(
        {TIER_RANK[entry.tier] for entry in candidates if TIER_RANK[entry.tier] >= target_rank}
    )
    for rank in ranks:
        at_tier = tuple(entry for entry in candidates if TIER_RANK[entry.tier] == rank)
        if at_tier:
            return min(at_tier, key=_cheapest_first)
    msg = "routing selection requires a candidate at or above the target tier"
    raise RuntimeError(msg)


class TieredRoutingPolicy:
    """Route each model turn by requirements, tier, and execution state.

    - *Vertical escalation*: when ``consecutive_no_progress`` reaches the
      configured stall threshold (repeated verification without score
      improvement), the next stronger tier with a compatible candidate is
      selected.
    - *Budget pressure*: when the runtime-reported remaining-budget fraction
      falls to the configured threshold, the cheapest compatible tier is
      selected. Budget pressure takes precedence over stall escalation —
      routing may bias cost downward but never touches budget enforcement.
    - *Horizontal fallback*: after a transient failure of the active model,
      a compatible model from a *different* provider at equal-or-stronger
      tier is selected when one exists — provider-scoped outages are the
      canonical transient failure, so a same-provider sibling is never a
      fallback; without a cross-provider alternative the active model is
      retained for the bounded retry (``FALLBACK_UNAVAILABLE``).
    - *Retention*: when nothing demands a change, the active model is kept
      (``ROUTE_RETAINED_CURRENT``), preserving adapter conversation state.

    ``default_tier`` is a *preference*, clamped to the strongest compatible
    tier the registry actually holds; ``ModelRequirements`` remains the hard
    gate. Initial selections are always reason-coded
    ``ROUTE_INITIAL_SELECTION`` — a first turn (including post-resume
    re-selection) never claims an escalation or de-escalation, though the
    stall/budget signals still shape which tier the first selection lands on.
    """

    def __init__(self, registry: ModelRegistry, *, config: RoutingPolicyConfig) -> None:
        self._registry = registry
        self._config = config

    def route(self, state: RunState, *, signals: RoutingSignals) -> RoutingDecision:
        candidates = self._registry.candidates(self._config.requirements)
        if not candidates:
            return RoutingDecision(
                reason_code=RouteReason.ROUTE_NO_COMPATIBLE_MODEL,
                model=None,
                tier=None,
            )
        current = (
            self._registry.entry_for(signals.current_model)
            if signals.current_model is not None
            else None
        )
        if current is not None and current not in candidates:
            # The active model does not satisfy the code-owned requirements;
            # never retain an incompatible model — re-select instead.
            current = None

        if signals.request_fallback and current is not None:
            alternatives = tuple(
                entry
                for entry in candidates
                if entry.capabilities.provider != current.capabilities.provider
                and TIER_RANK[entry.tier] >= TIER_RANK[current.tier]
            )
            if alternatives:
                chosen = _cheapest_at_or_above(alternatives, TIER_RANK[current.tier])
                return RoutingDecision(
                    reason_code=RouteReason.FALLBACK_TRANSIENT_FAILURE,
                    model=chosen.model,
                    tier=chosen.tier,
                )
            return RoutingDecision(
                reason_code=RouteReason.FALLBACK_UNAVAILABLE,
                model=current.model,
                tier=current.tier,
            )

        target_rank = (
            TIER_RANK[self._config.default_tier] if current is None else TIER_RANK[current.tier]
        )
        # The default tier is a preference: clamp it to the strongest
        # compatible tier the registry actually holds so an over-aspiring
        # wiring can never crash selection (requirements remain the hard gate).
        strongest_available = max(TIER_RANK[entry.tier] for entry in candidates)
        target_rank = min(target_rank, strongest_available)
        driver = (
            RouteReason.ROUTE_INITIAL_SELECTION
            if current is None
            else RouteReason.ROUTE_RETAINED_CURRENT
        )
        budget_pressure = (
            self._config.budget_pressure_remaining_fraction is not None
            and signals.budget_remaining_fraction is not None
            and signals.budget_remaining_fraction <= self._config.budget_pressure_remaining_fraction
        )
        cheapest_rank = min(TIER_RANK[entry.tier] for entry in candidates)
        if budget_pressure and cheapest_rank != target_rank:
            # Budget pressure wins only when it actually moves the target
            # downward; a no-op pressure signal must not suppress escalation.
            target_rank = cheapest_rank
            if current is not None:
                driver = RouteReason.DEESCALATED_BUDGET_PRESSURE
        elif state.consecutive_no_progress >= self._config.stall_escalation_threshold:
            stronger_ranks = [
                TIER_RANK[entry.tier] for entry in candidates if TIER_RANK[entry.tier] > target_rank
            ]
            if stronger_ranks:
                target_rank = min(stronger_ranks)
                if current is not None:
                    driver = RouteReason.ESCALATED_STALL

        chosen = _cheapest_at_or_above(candidates, target_rank)
        if current is not None and chosen.model is current.model:
            # The driver demanded a change but no better compatible candidate
            # exists; retaining is honest, so say so.
            driver = RouteReason.ROUTE_RETAINED_CURRENT
        return RoutingDecision(reason_code=driver, model=chosen.model, tier=chosen.tier)
