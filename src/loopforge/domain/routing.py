"""Model capability, tier, and routing vocabulary.

Mirrors the sandbox capability contract (``domain.security``): capability and
requirements vocabulary is code-owned domain knowledge, adapters honestly
advertise what their backing model can do, and capability matching fails
closed. Routing may optimize *which* model serves a turn — never runtime
authority: hard budgets, permissions, and stopping remain with
``ControlPolicy`` (AGENTS.md rule 12). This module is pure domain vocabulary
and carries zero provider semantics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ModelTier(StrEnum):
    """Strength/cost classes for vertical routing between models."""

    ECONOMY = "economy"
    STANDARD = "standard"
    ADVANCED = "advanced"


TIER_RANK: Final[dict[ModelTier, int]] = {
    ModelTier.ECONOMY: 0,
    ModelTier.STANDARD: 1,
    ModelTier.ADVANCED: 2,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCapabilities:
    """Provider/model capability metadata honestly advertised by an adapter.

    Cost rates are per one million tokens in USD; local providers report
    ``0.0``. Adapters advertise these truthfully: routing matches them against
    code-owned ``ModelRequirements`` and fails closed on mismatch, so an
    overclaimed capability is a wiring defect, not a runtime surprise.
    """

    provider: str
    model: str
    supports_tool_calls: bool
    context_window_tokens: int
    input_cost_usd_per_million: float = 0.0
    output_cost_usd_per_million: float = 0.0

    def __post_init__(self) -> None:
        if not self.provider.strip():
            msg = "model capabilities provider cannot be empty"
            raise ValueError(msg)
        if not self.model.strip():
            msg_2 = "model capabilities model cannot be empty"
            raise ValueError(msg_2)
        if not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.context_window_tokens, int
        ) or isinstance(self.context_window_tokens, bool):
            msg_5 = "model capabilities context window must be an integer"
            raise ValueError(msg_5)  # noqa: TRY004
        if self.context_window_tokens <= 0:
            msg_3 = "model capabilities context window must be positive"
            raise ValueError(msg_3)
        rates = (self.input_cost_usd_per_million, self.output_cost_usd_per_million)
        if not all(math.isfinite(rate) for rate in rates):
            msg_6 = "model capabilities cost rates must be finite"
            raise ValueError(msg_6)
        if self.input_cost_usd_per_million < 0 or self.output_cost_usd_per_million < 0:
            msg_4 = "model capabilities cost rates cannot be negative"
            raise ValueError(msg_4)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelRequirements:
    """Minimum capability properties a task or role requires from a routed model.

    Mirrors ``SandboxRequirements``: a code-owned contract declared by
    workloads. A model that cannot satisfy the requirements must never be
    routed to — matching fails closed, and a registry with no compatible
    model stops the run explicitly rather than degrading to an incompatible
    provider.
    """

    supports_tool_calls: bool = False
    min_context_window_tokens: int = 0
    max_input_cost_usd_per_million: float | None = None
    max_output_cost_usd_per_million: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.min_context_window_tokens, int
        ) or isinstance(self.min_context_window_tokens, bool):
            msg = "model requirements context window must be an integer"
            raise ValueError(msg)  # noqa: TRY004
        if self.min_context_window_tokens < 0:
            msg_2 = "model requirements context window cannot be negative"
            raise ValueError(msg_2)
        ceilings = (self.max_input_cost_usd_per_million, self.max_output_cost_usd_per_million)
        for ceiling in ceilings:
            if ceiling is None:
                continue
            if not math.isfinite(ceiling):
                msg_3 = "model requirements cost ceilings must be finite"
                raise ValueError(msg_3)
            if ceiling < 0:
                msg_4 = "model requirements cost ceilings cannot be negative"
                raise ValueError(msg_4)

    def missing_for(self, capabilities: ModelCapabilities) -> tuple[str, ...]:
        """Names of requirements ``capabilities`` fails to satisfy (empty = compatible)."""
        missing: list[str] = []
        if self.supports_tool_calls and not capabilities.supports_tool_calls:
            missing.append("supports_tool_calls")
        if capabilities.context_window_tokens < self.min_context_window_tokens:
            missing.append("min_context_window_tokens")
        if (
            self.max_input_cost_usd_per_million is not None
            and capabilities.input_cost_usd_per_million > self.max_input_cost_usd_per_million
        ):
            missing.append("max_input_cost_usd_per_million")
        if (
            self.max_output_cost_usd_per_million is not None
            and capabilities.output_cost_usd_per_million > self.max_output_cost_usd_per_million
        ):
            missing.append("max_output_cost_usd_per_million")
        return tuple(missing)


class RouteReason(StrEnum):
    """Closed machine-readable vocabulary for routing decisions.

    Every route/escalation/fallback carries one of these codes through
    telemetry (and, for ``ROUTE_NO_COMPATIBLE_MODEL``, the durable stop
    reason), so routing behavior is auditable without parsing prose.
    """

    ROUTE_INITIAL_SELECTION = "ROUTE_INITIAL_SELECTION"
    ROUTE_RETAINED_CURRENT = "ROUTE_RETAINED_CURRENT"
    ESCALATED_STALL = "ESCALATED_STALL"
    DEESCALATED_BUDGET_PRESSURE = "DEESCALATED_BUDGET_PRESSURE"
    FALLBACK_TRANSIENT_FAILURE = "FALLBACK_TRANSIENT_FAILURE"
    FALLBACK_UNAVAILABLE = "FALLBACK_UNAVAILABLE"
    ROUTE_NO_COMPATIBLE_MODEL = "ROUTE_NO_COMPATIBLE_MODEL"


@dataclass(frozen=True, slots=True, kw_only=True)
class RoutingPolicyConfig:
    """Code-owned knobs for deterministic tier routing.

    This config contains no authority: hard budgets, permissions, and
    stopping stay with ``ControlPolicy``. The budget-pressure threshold only
    biases selection toward cheaper tiers using a remaining-budget fraction
    the runtime computes from the code-owned budget limit; routing never
    enforces — or expands — budgets (AGENTS.md rule 12).
    """

    requirements: ModelRequirements
    default_tier: ModelTier = ModelTier.STANDARD
    stall_escalation_threshold: int = 2
    budget_pressure_remaining_fraction: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.stall_escalation_threshold, int
        ) or isinstance(self.stall_escalation_threshold, bool):
            msg = "stall escalation threshold must be an integer"
            raise ValueError(msg)  # noqa: TRY004
        if self.stall_escalation_threshold < 1:
            msg_2 = "stall escalation threshold must be positive"
            raise ValueError(msg_2)
        if self.budget_pressure_remaining_fraction is not None:
            if not math.isfinite(self.budget_pressure_remaining_fraction):
                msg_3 = "budget pressure fraction must be finite"
                raise ValueError(msg_3)
            if not 0.0 < self.budget_pressure_remaining_fraction <= 1.0:
                msg_4 = "budget pressure fraction must be in (0, 1]"
                raise ValueError(msg_4)
