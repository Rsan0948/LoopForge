"""Routing policy boundary for capability- and state-aware model selection.

The runtime consults a ``RoutingPolicyPort`` once per model turn. Policies
are deterministic and reason-coded; they select *which* registered adapter
serves the turn and may never touch budgets, permissions, or stopping
(AGENTS.md rule 12 — those stay with ``ControlPolicy``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.routing import ModelCapabilities, ModelTier, RouteReason
from loopforge.domain.state import RunState
from loopforge.ports.model import ModelPort


@dataclass(frozen=True, slots=True, kw_only=True)
class RoutingSignals:
    """Per-turn routing inputs the runtime derives from authoritative state.

    ``current_model`` identifies the active adapter (``None`` before the
    first selection). ``request_fallback`` marks that the previous turn
    failed transiently, so a horizontal provider fallback is preferred over
    retrying the same provider. ``budget_remaining_fraction`` is computed by
    the runtime from the code-owned budget limit — routing reads it to bias
    tier selection, never to enforce budgets (AGENTS.md rule 12).
    """

    current_model: ModelCapabilities | None = None
    model_failure_streak: int = 0
    request_fallback: bool = False
    budget_remaining_fraction: float | None = None

    def __post_init__(self) -> None:
        if self.current_model is not None and not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.current_model, ModelCapabilities
        ):
            msg_5 = "routing signals current model must be ModelCapabilities"
            raise TypeError(msg_5)
        if not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.model_failure_streak, int
        ) or isinstance(self.model_failure_streak, bool):
            msg = "routing signals failure streak must be an integer"
            raise ValueError(msg)  # noqa: TRY004
        if self.model_failure_streak < 0:
            msg_2 = "routing signals failure streak cannot be negative"
            raise ValueError(msg_2)
        if self.budget_remaining_fraction is not None:
            if not math.isfinite(self.budget_remaining_fraction):
                msg_3 = "routing signals budget fraction must be finite"
                raise ValueError(msg_3)
            if not 0.0 <= self.budget_remaining_fraction <= 1.0:
                msg_4 = "routing signals budget fraction must be in [0, 1]"
                raise ValueError(msg_4)


@dataclass(frozen=True, slots=True, kw_only=True)
class RoutingDecision:
    """One reason-coded routing outcome.

    ``model`` is ``None`` only with ``ROUTE_NO_COMPATIBLE_MODEL``: the
    runtime must fail closed (stop the run) rather than route a task to an
    incompatible model. Any other decision carries both a model and its
    registered tier.
    """

    reason_code: RouteReason
    model: ModelPort | None
    tier: ModelTier | None

    def __post_init__(self) -> None:
        # Coerce through the enum so plain-string reason codes cannot bypass
        # identity checks downstream (the PACS-011 failure-class lesson).
        object.__setattr__(self, "reason_code", RouteReason(self.reason_code))
        if self.tier is not None:
            object.__setattr__(self, "tier", ModelTier(self.tier))
        if self.model is None:
            if self.reason_code is not RouteReason.ROUTE_NO_COMPATIBLE_MODEL:
                msg = "only ROUTE_NO_COMPATIBLE_MODEL may carry no model"
                raise ValueError(msg)
            if self.tier is not None:
                msg_2 = "a model-less routing decision cannot carry a tier"
                raise ValueError(msg_2)
        else:
            if self.reason_code is RouteReason.ROUTE_NO_COMPATIBLE_MODEL:
                msg_4 = "ROUTE_NO_COMPATIBLE_MODEL cannot carry a model"
                raise ValueError(msg_4)
            if self.tier is None:
                msg_3 = "a routing decision with a model must carry its tier"
                raise ValueError(msg_3)


class RoutingPolicyPort(Protocol):
    def route(self, state: RunState, *, signals: RoutingSignals) -> RoutingDecision: ...
