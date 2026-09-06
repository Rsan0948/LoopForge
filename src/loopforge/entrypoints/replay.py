"""Composition root for counterfactual replay (PACS-017 M4).

Wires the deterministic re-drive runtime over a fresh store: model
proposals, tool results, and verification outcomes replay the recorded
stream in order (via ``extract_scripted_turns``), so the only free
variables are the knobs under study — an optional candidate
``ExecutionPolicy`` (routing, context-allocation bounds, verification
cadence) and the control budget. Immutable surfaces are not knobs:
permissions are reconstructed from the recorded tool contracts, and the
context allocator can only narrow inside the code-owned envelope.
"""

from __future__ import annotations

from loopforge.adapters.context import (
    AdaptiveContextBuilder,
    BudgetedContextBuilder,
    CharsPerTokenCounter,
)
from loopforge.adapters.model_registry import ModelRegistry, ModelRegistryEntry
from loopforge.adapters.routing import TieredRoutingPolicy
from loopforge.adapters.scripted import (
    FixedClock,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
    ScriptedVerifier,
)
from loopforge.application.counterfactual import extract_scripted_turns
from loopforge.application.runtime import Runtime
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.events import Event, ModelTurnRecorded
from loopforge.domain.policies import ExecutionPolicy
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.routing import (
    ModelCapabilities,
    ModelRequirements,
    ModelTier,
    RoutingPolicyConfig,
)
from loopforge.domain.types import BudgetLimit, Permission
from loopforge.ports.context import ContextBuilderPort
from loopforge.ports.state_store import StateStorePort

# The code-owned envelope every counterfactual context budget narrows
# within — identical to the repair entrypoint's wired default.
_ENVELOPE_BUDGET = ContextTokenBudget(max_tokens=4096, reserve_tokens=256)


def _recorded_capabilities(historical: tuple[Event, ...]) -> ModelCapabilities | None:
    """Replay the model identity the historical stream recorded, if any."""
    recorded = next(
        (event for event in historical if isinstance(event, ModelTurnRecorded)),
        None,
    )
    if recorded is None:
        return None
    return ModelCapabilities(
        provider=recorded.provider,
        model=recorded.model,
        supports_tool_calls=True,
        context_window_tokens=_ENVELOPE_BUDGET.max_tokens,
    )


def _router_for(
    historical: tuple[Event, ...],
    model: ScriptedModel,
    policy: ExecutionPolicy | None,
) -> TieredRoutingPolicy | None:
    """Wire routing only when the counterfactual or the history is routed."""
    candidate = policy.routing if policy is not None else None
    if candidate is None and not any(isinstance(event, ModelTurnRecorded) for event in historical):
        return None
    tier = candidate.default_tier if candidate is not None else ModelTier.ECONOMY
    registry = ModelRegistry((ModelRegistryEntry(model=model, tier=tier),))
    config = (
        candidate.for_requirements(ModelRequirements())
        if candidate is not None
        else RoutingPolicyConfig(requirements=ModelRequirements(), default_tier=tier)
    )
    return TieredRoutingPolicy(registry, config=config)


def build_counterfactual_runtime(
    historical: tuple[Event, ...],
    *,
    store: StateStorePort,
    budget: BudgetLimit,
    policy: ExecutionPolicy | None = None,
    cost_per_turn: float = 0.01,
) -> Runtime:
    """Build the deterministic re-drive runtime for one historical stream.

    The caller chooses the candidate policy (``None`` re-drives with the
    honest legacy cadence: every turn verified, fixed context budget, no
    routing bias beyond the recorded model identity).
    """
    if not historical:
        msg = "cannot build a counterfactual runtime from an empty stream"
        raise ValueError(msg)
    turns = extract_scripted_turns(historical)
    model = ScriptedModel(
        list(turns.proposals),
        cost_per_turn=cost_per_turn,
        capabilities=_recorded_capabilities(historical),
    )
    context: ContextBuilderPort = BudgetedContextBuilder(
        FixedClock(historical[0].occurred_at),
        CharsPerTokenCounter(),
        template=default_controller_template(),
        token_budget=_ENVELOPE_BUDGET,
    )
    if policy is not None:
        context = AdaptiveContextBuilder(
            context, bounds=policy.context_allocation, envelope=_ENVELOPE_BUDGET
        )
    return Runtime(
        model=model,
        router=_router_for(historical, model, policy),
        tools=ScriptedTools(list(turns.tool_results), metadata=list(turns.tool_metadata)),
        verifier=ScriptedVerifier(list(turns.verifications)),
        store=store,
        control=ControlPolicy(budget),
        permissions=PermissionPolicy(
            frozenset({item.required_permission for item in turns.tool_metadata})
            or frozenset({Permission.READ})
        ),
        reliability=ReliabilityPolicy(),
        context=context,
        clock=FixedClock(historical[0].occurred_at),
        sleeper=RecordingSleeper(),
        # The legacy cadence verifies every turn (ADR-0013); only an
        # explicit candidate policy narrows it.
        verify_read_only_turns=True if policy is None else policy.verify_read_only_turns,
    )
