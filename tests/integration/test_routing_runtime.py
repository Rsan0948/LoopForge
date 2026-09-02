"""Runtime routing integration: per-turn selection, mid-run swap, fail-closed stops.

All runs are deterministic: fake adapters only, in-memory stores, and the
recording telemetry sink. Live-model behavior is untouched (``tests/live/``).
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.model_registry import ModelRegistry, ModelRegistryEntry
from loopforge.adapters.routing import TieredRoutingPolicy
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedTools,
)
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext
from loopforge.domain.events import ActionProposed, ModelTurnRecorded, RunStopped
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy, RetrySettings
from loopforge.domain.routing import (
    ModelCapabilities,
    ModelRequirements,
    ModelTier,
    RouteReason,
    RoutingPolicyConfig,
)
from loopforge.domain.state import RunState
from loopforge.domain.telemetry import SpanName
from loopforge.domain.tooling import (
    ApprovalClass,
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
    StopReason,
    UsageDelta,
)
from loopforge.ports.model import ModelContractError, ModelFailureClass, ModelTurn, ModelTurnError
from loopforge.ports.routing import RoutingSignals
from loopforge.ports.tools import ToolResult

NOW = datetime(2026, 8, 28, tzinfo=UTC)


class RoutedFakeModel:
    """Scripted fake with honest, configurable capability identity."""

    def __init__(
        self,
        script: list[ModelTurnError | ActionProposal],
        *,
        provider: str,
        name: str,
        tool_calls: bool = True,
        cost_per_turn: float = 0.01,
    ) -> None:
        self._script = deque(script)
        self.calls = 0
        self._capabilities = ModelCapabilities(
            provider=provider,
            model=name,
            supports_tool_calls=tool_calls,
            context_window_tokens=8192,
        )
        self._cost_per_turn = cost_per_turn

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def propose_action(self, context: ModelContext) -> ModelTurn:
        del context
        self.calls += 1
        step = self._script.popleft()
        if isinstance(step, ModelTurnError):
            raise step
        return ModelTurn(
            action=step,
            usage=UsageDelta(cost_usd=self._cost_per_turn, input_tokens=10, output_tokens=5),
        )


def _metadata(name: str = "inspect") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _router(
    entries: tuple[tuple[RoutedFakeModel, ModelTier], ...],
    *,
    requirements: ModelRequirements | None = None,
    default_tier: ModelTier = ModelTier.STANDARD,
    stall_threshold: int = 2,
    budget_fraction: float | None = None,
) -> TieredRoutingPolicy:
    registry = ModelRegistry(
        tuple(ModelRegistryEntry(model=model, tier=tier) for model, tier in entries)
    )
    return TieredRoutingPolicy(
        registry,
        config=RoutingPolicyConfig(
            requirements=requirements or ModelRequirements(supports_tool_calls=True),
            default_tier=default_tier,
            stall_escalation_threshold=stall_threshold,
            budget_pressure_remaining_fraction=budget_fraction,
        ),
    )


def _runtime(  # noqa: PLR0913 - keyword-only wiring keeps every runtime dependency explicit
    model: RoutedFakeModel,
    *,
    router: TieredRoutingPolicy | None = None,
    telemetry: InMemoryTelemetry | None = None,
    store: InMemoryEventStore | None = None,
    sleeper: RecordingSleeper | None = None,
    observations: list[str] | None = None,
    budget: BudgetLimit | None = None,
) -> Runtime:
    return Runtime(
        model=model,
        tools=ScriptedTools(
            [
                ToolResult(ok=True, observation=text)
                for text in (observations or ["all tests pass"])
            ],
            metadata=[_metadata()],
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=store or InMemoryEventStore(),
        control=ControlPolicy(budget or BudgetLimit(max_cost_usd=10.0, max_iterations=8)),
        permissions=PermissionPolicy(frozenset({Permission.READ, Permission.LOCAL_WRITE})),
        reliability=ReliabilityPolicy(retry=RetrySettings(max_attempts=3)),
        context=BasicContextBuilder(FixedClock(NOW)),
        clock=FixedClock(NOW),
        sleeper=sleeper or RecordingSleeper(),
        telemetry=telemetry,
        router=router,
    )


def _transient() -> ModelTurnError:
    return ModelTurnError(ModelFailureClass.TRANSIENT, "MODEL_UNAVAILABLE", "provider down")


def _route_reasons(telemetry: InMemoryTelemetry) -> list[str]:
    return [
        str(span.attributes["loopforge.route.reason_code"])
        for span in telemetry.spans
        if span.name == SpanName.MODEL_ROUTE.value
    ]


def _stopped_summary(store: InMemoryEventStore, run_id: RunId) -> str:
    stopped = [event for event in store.events_for(run_id) if isinstance(event, RunStopped)]
    assert len(stopped) == 1
    return stopped[0].summary


# --- Per-turn routing telemetry --------------------------------------------------------


def test_routed_run_emits_reason_coded_route_spans() -> None:
    model = RoutedFakeModel(
        [ActionProposal(ActionId("a1"), "inspect", {})], provider="p", name="only"
    )
    telemetry = InMemoryTelemetry()
    runtime = _runtime(
        model,
        router=_router(((model, ModelTier.STANDARD),)),
        telemetry=telemetry,
    )
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    assert _route_reasons(telemetry) == [RouteReason.ROUTE_INITIAL_SELECTION.value]
    route_span = next(span for span in telemetry.spans if span.name == SpanName.MODEL_ROUTE.value)
    assert route_span.attributes["loopforge.route.provider"] == "p"
    assert route_span.attributes["loopforge.route.model"] == "only"
    assert route_span.attributes["loopforge.route.tier"] == ModelTier.STANDARD.value


def test_run_without_router_emits_no_route_spans() -> None:
    model = RoutedFakeModel(
        [ActionProposal(ActionId("a1"), "inspect", {})], provider="p", name="direct"
    )
    telemetry = InMemoryTelemetry()
    runtime = _runtime(model, telemetry=telemetry)
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    assert not [s for s in telemetry.spans if s.name == SpanName.MODEL_ROUTE.value]


# --- Fail-closed capability gating -------------------------------------------------------


def test_task_requiring_unsupported_capability_is_never_routed() -> None:
    model = RoutedFakeModel(
        [ActionProposal(ActionId("a1"), "inspect", {})],
        provider="p",
        name="no-tools",
        tool_calls=False,
    )
    store = InMemoryEventStore()
    runtime = _runtime(
        model,
        router=_router(((model, ModelTier.STANDARD),)),
        store=store,
    )
    state = runtime.run("objective")
    assert state.status is RunStatus.FAILED
    assert state.stop_reason is StopReason.FAILURE
    # The incompatible model was never invoked, and the reason code is durable.
    assert model.calls == 0
    summary = _stopped_summary(store, state.run_id)
    assert RouteReason.ROUTE_NO_COMPATIBLE_MODEL.value in summary
    # Terminal states are durable: resume is a no-op.
    assert runtime.resume(state.run_id).status is RunStatus.FAILED
    assert model.calls == 0


# --- Horizontal fallback: mid-run model swap ----------------------------------------------


def test_transient_failure_swaps_to_compatible_provider_mid_run() -> None:
    failing = RoutedFakeModel([_transient()], provider="provider-a", name="standard")
    fallback = RoutedFakeModel(
        [ActionProposal(ActionId("a1"), "inspect", {})],
        provider="provider-b",
        name="standard",
    )
    telemetry = InMemoryTelemetry()
    sleeper = RecordingSleeper()
    runtime = _runtime(
        failing,
        router=_router(
            (
                (failing, ModelTier.STANDARD),
                (fallback, ModelTier.STANDARD),
            )
        ),
        telemetry=telemetry,
        sleeper=sleeper,
    )
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    assert failing.calls == 1
    assert fallback.calls == 1
    assert _route_reasons(telemetry) == [
        RouteReason.ROUTE_INITIAL_SELECTION.value,
        RouteReason.FALLBACK_TRANSIENT_FAILURE.value,
    ]
    assert sleeper.delays == [0.25]


def test_fallback_unavailable_retains_model_and_preserves_retry_semantics() -> None:
    model = RoutedFakeModel(
        [_transient(), ActionProposal(ActionId("a1"), "inspect", {})],
        provider="p",
        name="only",
    )
    telemetry = InMemoryTelemetry()
    runtime = _runtime(
        model,
        router=_router(((model, ModelTier.STANDARD),)),
        telemetry=telemetry,
    )
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    assert model.calls == 2
    assert _route_reasons(telemetry) == [
        RouteReason.ROUTE_INITIAL_SELECTION.value,
        RouteReason.FALLBACK_UNAVAILABLE.value,
    ]


# --- Vertical escalation on stalls ----------------------------------------------------------


def test_stall_escalates_to_stronger_tier_mid_run() -> None:
    economy = RoutedFakeModel(
        [
            ActionProposal(ActionId("e1"), "inspect", {}),
            ActionProposal(ActionId("e2"), "inspect", {}),
            ActionProposal(ActionId("e3"), "inspect", {}),
        ],
        provider="p",
        name="economy",
    )
    advanced = RoutedFakeModel(
        [ActionProposal(ActionId("x1"), "inspect", {})],
        provider="p",
        name="advanced",
    )
    telemetry = InMemoryTelemetry()
    runtime = _runtime(
        economy,
        router=_router(
            (
                (economy, ModelTier.ECONOMY),
                (advanced, ModelTier.ADVANCED),
            ),
            default_tier=ModelTier.ECONOMY,
        ),
        telemetry=telemetry,
        observations=["still failing", "still failing", "still failing", "all tests pass"],
    )
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    # Two no-progress cycles accumulated, then the router escalated.
    assert economy.calls == 3
    assert advanced.calls == 1
    assert _route_reasons(telemetry) == [
        RouteReason.ROUTE_INITIAL_SELECTION.value,
        RouteReason.ROUTE_RETAINED_CURRENT.value,
        RouteReason.ROUTE_RETAINED_CURRENT.value,
        RouteReason.ESCALATED_STALL.value,
    ]


# --- Budget pressure de-escalation --------------------------------------------------------------


def test_budget_pressure_deescalates_to_cheaper_tier_mid_run() -> None:
    advanced = RoutedFakeModel(
        [ActionProposal(ActionId("x1"), "inspect", {})],
        provider="p",
        name="advanced",
        cost_per_turn=0.8,
    )
    economy = RoutedFakeModel(
        [ActionProposal(ActionId("e1"), "inspect", {})],
        provider="p",
        name="economy",
    )
    telemetry = InMemoryTelemetry()
    runtime = _runtime(
        advanced,
        router=_router(
            (
                (economy, ModelTier.ECONOMY),
                (advanced, ModelTier.ADVANCED),
            ),
            default_tier=ModelTier.ADVANCED,
            budget_fraction=0.3,
        ),
        telemetry=telemetry,
        observations=["still failing", "all tests pass"],
        budget=BudgetLimit(max_cost_usd=1.0, max_iterations=8),
    )
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    assert advanced.calls == 1
    assert economy.calls == 1
    assert _route_reasons(telemetry) == [
        RouteReason.ROUTE_INITIAL_SELECTION.value,
        RouteReason.DEESCALATED_BUDGET_PRESSURE.value,
    ]


# --- Routing policy contract boundary -------------------------------------------


class _GarbageRouter:
    def route(self, state: RunState, *, signals: RoutingSignals) -> str:
        del state, signals
        return "not a RoutingDecision"


def test_routing_policy_contract_violation_fails_loudly() -> None:
    model = RoutedFakeModel([ActionProposal(ActionId("a1"), "inspect", {})], provider="p", name="m")
    runtime = _runtime(
        model,
        router=_GarbageRouter(),  # pyright: ignore[reportArgumentType]
    )
    with pytest.raises(ModelContractError, match="expected RoutingDecision"):
        runtime.run("objective")


# --- PACS-015: per-turn model-identity provenance ---------------------------------


def _model_turns(store: InMemoryEventStore, run_id: RunId) -> list[ModelTurnRecorded]:
    return [event for event in store.events_for(run_id) if isinstance(event, ModelTurnRecorded)]


def test_every_successful_turn_records_its_model_identity() -> None:
    model = RoutedFakeModel(
        [
            ActionProposal(ActionId("a1"), "inspect", {}),
            ActionProposal(ActionId("a2"), "inspect", {}),
        ],
        provider="p",
        name="direct",
    )
    store = InMemoryEventStore()
    runtime = _runtime(model, store=store, observations=["still failing", "all tests pass"])

    state = runtime.run("objective")

    assert state.status is RunStatus.SUCCEEDED
    turns = _model_turns(store, state.run_id)
    assert [(turn.provider, turn.model) for turn in turns] == [("p", "direct"), ("p", "direct")]
    assert [turn.action_id for turn in turns] == [ActionId("a1"), ActionId("a2")]
    # Each identity record lands immediately before the ActionProposed it yielded.
    events = store.events_for(state.run_id)
    proposed = [event for event in events if isinstance(event, ActionProposed)]
    assert len(proposed) == 2
    for proposal in proposed:
        predecessor = events[proposal.sequence - 2]
        assert isinstance(predecessor, ModelTurnRecorded)
        assert predecessor.action_id == proposal.proposal.action_id


def test_failed_turn_records_nothing_and_the_fallback_turn_records_the_fallback_model() -> None:
    failing = RoutedFakeModel([_transient()], provider="provider-a", name="standard")
    fallback = RoutedFakeModel(
        [ActionProposal(ActionId("a1"), "inspect", {})],
        provider="provider-b",
        name="standard",
    )
    store = InMemoryEventStore()
    runtime = _runtime(
        failing,
        router=_router(
            (
                (failing, ModelTier.STANDARD),
                (fallback, ModelTier.STANDARD),
            )
        ),
        store=store,
        sleeper=RecordingSleeper(),
    )

    state = runtime.run("objective")

    assert state.status is RunStatus.SUCCEEDED
    turns = _model_turns(store, state.run_id)
    # The transient failure never produced a turn; the one successful turn is
    # attributed to the fallback model that actually produced it.
    assert [(turn.provider, turn.model) for turn in turns] == [("provider-b", "standard")]


class _BrokenCapabilitiesModel(RoutedFakeModel):
    @property
    def capabilities(self) -> ModelCapabilities:
        # Deliberate port-contract violation: the runtime must fail loudly
        # instead of recording a garbage model identity into the durable stream.
        return cast(Any, object())


def test_adapter_with_broken_capabilities_fails_closed() -> None:
    model = _BrokenCapabilitiesModel(
        [ActionProposal(ActionId("a1"), "inspect", {})], provider="p", name="broken"
    )
    store = InMemoryEventStore()
    runtime = _runtime(model, store=store)

    with pytest.raises(ModelContractError, match="expected ModelCapabilities"):
        runtime.run("objective")

    # Nothing about the turn entered the durable stream.
    (record,) = store.list_runs()
    assert _model_turns(store, record.run_id) == []
