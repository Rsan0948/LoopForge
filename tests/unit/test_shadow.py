"""Shadow-policy evaluation tests (PACS-017 M3).

The candidate policy's decisions are durable evidence-only records: the
catalog extension is pinned through every touchpoint, the runtime journals
shadow advice at its three decision points, the active path stays
byte-identical with or without a shadow, shadow advice is never enacted,
and advisor failure is honest absence — never a disturbance.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from loopforge.adapters.context import BudgetedContextBuilder, CharsPerTokenCounter
from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.model_registry import ModelRegistry, ModelRegistryEntry
from loopforge.adapters.routing import TieredRoutingPolicy
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
)
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.runtime import Runtime
from loopforge.application.shadow import CandidateShadowAdvisor
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    Event,
    ModelTurnRecorded,
    PlanCreated,
    RunStarted,
    RunStopped,
    ShadowDecisionRecorded,
    ToolSucceeded,
)
from loopforge.domain.policies import (
    ContextAllocationBounds,
    ExecutionPolicy,
    PolicyRoutingKnobs,
    ShadowDecisionKind,
)
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy, ToolFailureClass
from loopforge.domain.routing import (
    ModelCapabilities,
    ModelRequirements,
    ModelTier,
    RoutingPolicyConfig,
)
from loopforge.domain.state import InvalidTransitionError, RunState, replay
from loopforge.domain.tooling import (
    ApprovalClass,
    DataSensitivity,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import (
    ActionId,
    BudgetLimit,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    RunStatus,
    StopReason,
)
from loopforge.ports.routing import RoutingSignals
from loopforge.ports.shadow import ShadowAdvice
from loopforge.ports.tools import ToolResult

NOW = datetime(2026, 9, 5, tzinfo=UTC)
RUN = RunId("shadow-run")
CODEC = JsonEventCodec()

CANDIDATE = ExecutionPolicy(
    policy_id="candidate-shadow",
    version=2,
    routing=PolicyRoutingKnobs(default_tier=ModelTier.ADVANCED),
    context_allocation=ContextAllocationBounds(
        floor_tokens=1024, ceiling_tokens=4096, reserve_tokens=256, step_tokens=512
    ),
)


def _event(sequence: int = 1, **overrides: Any) -> ShadowDecisionRecorded:
    base: dict[str, Any] = {
        "event_id": EventId("e-shadow"),
        "run_id": RUN,
        "occurred_at": NOW,
        "sequence": sequence,
        "policy_id": "candidate-shadow",
        "policy_version": 2,
        "kind": ShadowDecisionKind.MODEL_ROUTE,
        "decision": "ollama/devstral tier=advanced",
        "basis": "reason_code=ROUTE_INITIAL_SELECTION",
    }
    base.update(overrides)
    return ShadowDecisionRecorded(**base)


def _metadata(name: str = "inspect", permission: Permission = Permission.READ) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=permission,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
        sensitivity=DataSensitivity.INTERNAL,
    )


def _caps(model: str) -> ModelCapabilities:
    return ModelCapabilities(
        provider="scripted",
        model=model,
        supports_tool_calls=True,
        context_window_tokens=4096,
    )


# --- Event validation: allow + deny --------------------------------------------


def test_valid_shadow_event_constructs() -> None:
    event = _event()
    assert event.policy_id == "candidate-shadow"
    assert event.policy_version == 2
    assert event.kind is ShadowDecisionKind.MODEL_ROUTE


@pytest.mark.parametrize("policy_id", ["", "  ", "a/b", "..", "-x", "x" * 65])
def test_shadow_event_rejects_unsafe_policy_ids(policy_id: str) -> None:
    with pytest.raises(ValueError, match="policy_id"):
        _event(policy_id=policy_id)


@pytest.mark.parametrize("version", [0, -1, True, 1.5])
def test_shadow_event_rejects_bad_versions(version: object) -> None:
    with pytest.raises(ValueError, match="policy_version"):
        _event(policy_version=version)


def test_shadow_event_rejects_non_closed_kind() -> None:
    with pytest.raises(TypeError, match="ShadowDecisionKind"):
        _event(kind="model_route")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision", ""),
        ("decision", "   "),
        ("decision", "x" * 513),
        ("decision", "bad\x00text"),
        ("basis", ""),
        ("basis", "y" * 513),
        ("basis", "bad\x7ftext"),
    ],
)
def test_shadow_event_rejects_bad_decision_text(field: str, value: str) -> None:
    overrides: dict[str, Any] = {field: value}
    with pytest.raises(ValueError, match="shadow decision"):
        _event(**overrides)


@pytest.mark.parametrize("kind", list(ShadowDecisionKind))
def test_shadow_event_codec_round_trip_per_kind(kind: ShadowDecisionKind) -> None:
    event = _event(kind=kind)
    assert CODEC.decode(CODEC.encode(event)) == event


def test_shadow_event_decode_rejects_unknown_kind() -> None:
    payload = json.loads(CODEC.encode(_event()))
    payload["event"]["kind"] = "mind_control"
    with pytest.raises(ValueError, match="mind_control"):
        CODEC.decode(json.dumps(payload))


# --- Reducer: evidence-only projection + status gating ---------------------------


def _fields(sequence: int) -> dict[str, Any]:
    return {
        "event_id": EventId(f"e{sequence}"),
        "run_id": RUN,
        "occurred_at": NOW,
        "sequence": sequence,
    }


def _ready_stream() -> tuple[Event, ...]:
    proposal = ActionProposal(ActionId("a1"), "inspect", {})
    return (
        RunStarted(**_fields(1), objective="probe"),
        PlanCreated(**_fields(2), plan="plan"),
        ActionProposed(**_fields(3), proposal=proposal),
        ActionAuthorized(**_fields(4), proposal=proposal, tool_metadata=_metadata()),
    )


def test_reducer_projects_shadow_events_without_any_control_state_effect() -> None:
    prefix = _ready_stream()[:2]
    plain = replay(RUN, prefix)
    shadowed = replay(RUN, (*prefix, _event(3)))
    # Every projected field is identical; only the event-count version moves.
    assert replace(shadowed, version=plain.version) == plain


def test_shadow_event_is_allowed_in_ready_and_verifying() -> None:
    acting_prefix = _ready_stream()
    ready_prefix = acting_prefix[:2]
    replay(RUN, (*ready_prefix, _event(3)))  # READY: allowed
    verifying_prefix = (
        *acting_prefix,
        ToolSucceeded(**_fields(5), action_id=ActionId("a1"), observation="done", attempt=1),
    )
    replay(RUN, (*verifying_prefix, _event(6)))  # VERIFYING: allowed


def test_shadow_event_is_denied_in_planning_and_acting() -> None:
    with pytest.raises(InvalidTransitionError, match="ShadowDecisionRecorded"):
        replay(RUN, (_ready_stream()[0], _event(2)))  # PLANNING
    with pytest.raises(InvalidTransitionError, match="ShadowDecisionRecorded"):
        replay(RUN, (*_ready_stream(), _event(5)))  # ACTING


def test_shadow_event_is_denied_after_terminal_stop() -> None:
    terminal = (
        *_ready_stream()[:2],
        RunStopped(**_fields(3), reason=StopReason.CANCELLED, summary="halt"),
    )
    with pytest.raises(InvalidTransitionError, match="terminal run"):
        replay(RUN, (*terminal, _event(4)))


# --- CandidateShadowAdvisor behavior ----------------------------------------------


def _advisor(
    policy: ExecutionPolicy = CANDIDATE,
    *,
    router: TieredRoutingPolicy | None = None,
    accounting_source: object = None,
) -> CandidateShadowAdvisor:
    if router is None:
        model = ScriptedModel([], capabilities=_caps("scripted-advanced"))
        router = TieredRoutingPolicy(
            ModelRegistry((ModelRegistryEntry(model=model, tier=ModelTier.ADVANCED),)),
            config=policy.routing.for_requirements(ModelRequirements()),
        )
    return CandidateShadowAdvisor(policy, router=router, accounting_source=accounting_source)


def test_advise_route_reports_the_candidate_choice_and_reason() -> None:
    advice = _advisor().advise_route(RunState(run_id=RUN), signals=RoutingSignals())
    assert advice.kind is ShadowDecisionKind.MODEL_ROUTE
    assert advice.decision == "scripted/scripted-advanced tier=advanced"
    assert advice.basis == "reason_code=ROUTE_INITIAL_SELECTION"


def test_advise_route_reports_no_compatible_model_honestly() -> None:
    incompatible = ScriptedModel(
        [],
        capabilities=ModelCapabilities(
            provider="scripted",
            model="no-tools",
            supports_tool_calls=False,
            context_window_tokens=4096,
        ),
    )
    router = TieredRoutingPolicy(
        ModelRegistry((ModelRegistryEntry(model=incompatible, tier=ModelTier.ECONOMY),)),
        config=RoutingPolicyConfig(
            requirements=ModelRequirements(supports_tool_calls=True),
            default_tier=ModelTier.ECONOMY,
        ),
    )
    advice = _advisor(router=router).advise_route(RunState(run_id=RUN), signals=RoutingSignals())
    assert advice.decision == "no-compatible-model"
    assert advice.basis == "reason_code=ROUTE_NO_COMPATIBLE_MODEL"


def _dropping_accounting_source() -> BudgetedContextBuilder:
    builder = BudgetedContextBuilder(
        FixedClock(NOW),
        CharsPerTokenCounter(),
        template=default_controller_template(),
        token_budget=ContextTokenBudget(max_tokens=1024),
    )
    builder.build_context(
        RunState(
            run_id=RUN,
            status=RunStatus.READY,
            objective="repair",
            plan="p" * 24000,
            last_observation="o" * 24000,
            last_reflection="r" * 24000,
        )
    )
    return builder


def test_advise_context_budget_mirrors_the_allocator_trajectory() -> None:
    advisor = _advisor(accounting_source=_dropping_accounting_source())
    first = advisor.advise_context_budget(RunState(run_id=RUN))
    assert first.kind is ShadowDecisionKind.CONTEXT_BUDGET
    assert first.decision == "max_tokens=1024 reserve_tokens=256"
    assert first.basis.startswith("dropped_over_budget=True utilization_fraction=")
    # The advisor adapts its shadow budget exactly like the real allocator.
    second = advisor.advise_context_budget(RunState(run_id=RUN))
    assert second.decision == "max_tokens=1536 reserve_tokens=256"


def test_advise_context_budget_without_accounting_is_honest_absence() -> None:
    advice = _advisor(accounting_source=None).advise_context_budget(RunState(run_id=RUN))
    assert advice.decision == "max_tokens=1024 reserve_tokens=256"
    assert advice.basis == "accounting=absent"


@pytest.mark.parametrize(
    ("verify_read_only", "tool_failure", "expected"),
    [
        (False, False, "skip"),
        (True, False, "verify"),
        (False, True, "verify"),
    ],
)
def test_advise_verification_cadence_applies_the_candidate_knob(
    verify_read_only: bool, tool_failure: bool, expected: str
) -> None:
    policy = ExecutionPolicy(
        policy_id="cadence-candidate",
        version=1,
        context_allocation=ContextAllocationBounds(floor_tokens=1024, ceiling_tokens=4096),
        verify_read_only_turns=verify_read_only,
    )
    state = RunState(
        run_id=RUN,
        current_tool_metadata=_metadata(),
        last_tool_failure_class=ToolFailureClass.TRANSIENT if tool_failure else None,
    )
    advice = _advisor(policy).advise_verification_cadence(state)
    assert advice.kind is ShadowDecisionKind.VERIFICATION_CADENCE
    assert advice.decision == expected
    assert f"verify_read_only_turns={verify_read_only}" in advice.basis
    assert "read_only_turn=True" in advice.basis


def test_advise_verification_cadence_verifies_without_read_metadata() -> None:
    advice = _advisor().advise_verification_cadence(RunState(run_id=RUN))
    assert advice.decision == "verify"
    assert "read_only_turn=False" in advice.basis


# --- Runtime integration ------------------------------------------------------------


def _runtime(
    *,
    shadow: object | None = None,
    telemetry: object | None = None,
) -> tuple[Runtime, InMemoryEventStore]:
    """A routed scripted runtime: one READ turn, observation verifier passes."""
    economy = ScriptedModel(
        [ActionProposal(ActionId("a1"), "inspect", {})], capabilities=_caps("economy-model")
    )
    advanced = ScriptedModel(
        [ActionProposal(ActionId("a2"), "inspect", {})], capabilities=_caps("advanced-model")
    )
    registry = ModelRegistry(
        (
            ModelRegistryEntry(model=economy, tier=ModelTier.ECONOMY),
            ModelRegistryEntry(model=advanced, tier=ModelTier.ADVANCED),
        )
    )
    router = TieredRoutingPolicy(
        registry,
        config=RoutingPolicyConfig(
            requirements=ModelRequirements(), default_tier=ModelTier.ECONOMY
        ),
    )
    store = InMemoryEventStore()
    runtime = Runtime(
        model=economy,
        router=router,
        tools=ScriptedTools([ToolResult(ok=True, observation="all done")], metadata=[_metadata()]),
        verifier=ObservationContainsVerifier("done"),
        store=store,
        control=ControlPolicy(BudgetLimit(max_cost_usd=5.0, max_iterations=4)),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(),
        context=BudgetedContextBuilder(
            FixedClock(NOW),
            CharsPerTokenCounter(),
            template=default_controller_template(),
            token_budget=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
        ),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
        telemetry=telemetry,  # pyright: ignore[reportArgumentType]
        # Observation-based verifier: the legacy cadence is the honest wiring
        # (ADR-0013), so the active runtime verifies every turn.
        verify_read_only_turns=True,
        shadow=shadow,  # pyright: ignore[reportArgumentType]
    )
    return runtime, store


def _shadow_advisor_for(runtime: Runtime) -> CandidateShadowAdvisor:
    advanced = ScriptedModel(
        [ActionProposal(ActionId("a2"), "inspect", {})], capabilities=_caps("advanced-model")
    )
    economy = ScriptedModel(
        [ActionProposal(ActionId("a1"), "inspect", {})], capabilities=_caps("economy-model")
    )
    registry = ModelRegistry(
        (
            ModelRegistryEntry(model=economy, tier=ModelTier.ECONOMY),
            ModelRegistryEntry(model=advanced, tier=ModelTier.ADVANCED),
        )
    )
    router = TieredRoutingPolicy(
        registry,
        config=CANDIDATE.routing.for_requirements(ModelRequirements()),
    )
    return CandidateShadowAdvisor(CANDIDATE, router=router, accounting_source=runtime.context)


def test_runtime_journals_shadow_decisions_at_all_three_decision_points() -> None:
    runtime, store = _runtime()
    advisor = _shadow_advisor_for(runtime)
    runtime.shadow = advisor
    state = runtime.run("probe the shadow")
    assert state.status is RunStatus.SUCCEEDED
    shadow_events = [
        event
        for event in store.events_for(state.run_id)
        if isinstance(event, ShadowDecisionRecorded)
    ]
    kinds = [event.kind for event in shadow_events]
    assert ShadowDecisionKind.MODEL_ROUTE in kinds
    assert ShadowDecisionKind.CONTEXT_BUDGET in kinds
    assert ShadowDecisionKind.VERIFICATION_CADENCE in kinds
    assert all(event.policy_id == "candidate-shadow" for event in shadow_events)
    assert all(event.policy_version == 2 for event in shadow_events)


def _canonical(events: tuple[Event, ...], run_id: RunId) -> list[dict[str, Any]]:
    """Payload comparison projection: drop volatile fields and shadow evidence."""
    canonical: list[dict[str, Any]] = []
    for event in events:
        if isinstance(event, ShadowDecisionRecorded):
            continue
        body = json.loads(CODEC.encode(event).replace(str(run_id), "RUN"))["event"]
        for key in ("event_id", "run_id", "occurred_at", "sequence", "caused_by"):
            body.pop(key)
        canonical.append(body)
    return canonical


def test_active_path_is_byte_identical_with_and_without_a_shadow() -> None:
    plain_runtime, plain_store = _runtime()
    plain_state = plain_runtime.run("probe the shadow")
    shadowed_runtime, shadowed_store = _runtime()
    shadowed_runtime.shadow = _shadow_advisor_for(shadowed_runtime)
    shadowed_state = shadowed_runtime.run("probe the shadow")
    assert _canonical(
        shadowed_store.events_for(shadowed_state.run_id), shadowed_state.run_id
    ) == _canonical(plain_store.events_for(plain_state.run_id), plain_state.run_id)


def test_shadow_advice_is_never_enacted() -> None:
    runtime, store = _runtime()
    runtime.shadow = _shadow_advisor_for(runtime)
    state = runtime.run("probe the shadow")
    events = store.events_for(state.run_id)
    # The candidate router prefers ADVANCED; the active policy serves ECONOMY.
    route_advice = next(
        event
        for event in events
        if isinstance(event, ShadowDecisionRecorded)
        and event.kind is ShadowDecisionKind.MODEL_ROUTE
    )
    assert route_advice.decision == "scripted/advanced-model tier=advanced"
    turns = [event for event in events if isinstance(event, ModelTurnRecorded)]
    assert turns
    assert all(event.model == "economy-model" for event in turns)
    # The candidate cadence would skip verification; the active runtime verified.
    cadence_advice = next(
        event
        for event in events
        if isinstance(event, ShadowDecisionRecorded)
        and event.kind is ShadowDecisionKind.VERIFICATION_CADENCE
    )
    assert cadence_advice.decision == "skip"
    assert any(type(event).__name__ == "VerificationPassed" for event in events)


class _RaisingAdvisor:
    @property
    def policy(self) -> ExecutionPolicy:
        return CANDIDATE

    def advise_route(self, state: RunState, *, signals: RoutingSignals) -> ShadowAdvice:
        del state, signals
        msg = "shadow exploded"
        raise RuntimeError(msg)

    def advise_context_budget(self, state: RunState) -> ShadowAdvice:
        del state
        msg = "shadow exploded"
        raise RuntimeError(msg)

    def advise_verification_cadence(self, state: RunState) -> ShadowAdvice:
        del state
        msg = "shadow exploded"
        raise RuntimeError(msg)


class _ContractViolatingAdvisor:
    @property
    def policy(self) -> ExecutionPolicy:
        return CANDIDATE

    def advise_route(self, state: RunState, *, signals: RoutingSignals) -> object:
        del state, signals
        return "not-an-advice"

    def advise_context_budget(self, state: RunState) -> object:
        del state
        return 42

    def advise_verification_cadence(self, state: RunState) -> object:
        del state
        return None


class _InvalidPayloadAdvisor:
    @property
    def policy(self) -> ExecutionPolicy:
        return CANDIDATE

    def advise_route(self, state: RunState, *, signals: RoutingSignals) -> ShadowAdvice:
        del state, signals
        return ShadowAdvice(
            kind=ShadowDecisionKind.MODEL_ROUTE,
            decision="bad\x00decision",
            basis="reason_code=X",
        )

    def advise_context_budget(self, state: RunState) -> ShadowAdvice:
        del state
        return ShadowAdvice(kind=ShadowDecisionKind.CONTEXT_BUDGET, decision="x" * 9999, basis="b")

    def advise_verification_cadence(self, state: RunState) -> ShadowAdvice:
        del state
        return ShadowAdvice(
            kind=ShadowDecisionKind.VERIFICATION_CADENCE, decision="skip", basis=" "
        )


@pytest.mark.parametrize(
    "advisor_factory",
    [_RaisingAdvisor, _ContractViolatingAdvisor, _InvalidPayloadAdvisor],
    ids=["raising", "contract-violating", "invalid-payload"],
)
def test_shadow_failure_is_honest_absence_and_never_disturbs_the_run(
    advisor_factory: type,
) -> None:
    telemetry = InMemoryTelemetry()
    runtime, store = _runtime(shadow=advisor_factory(), telemetry=telemetry)
    state = runtime.run("probe the shadow")
    assert state.status is RunStatus.SUCCEEDED
    events = store.events_for(state.run_id)
    assert not any(isinstance(event, ShadowDecisionRecorded) for event in events)
    assert any(log.message == "shadow advisor failure" for log in telemetry.logs)


def test_unshadowed_runtime_records_no_shadow_events() -> None:
    runtime, store = _runtime()
    state = runtime.run("probe the shadow")
    assert state.status is RunStatus.SUCCEEDED
    assert not any(
        isinstance(event, ShadowDecisionRecorded) for event in store.events_for(state.run_id)
    )


def test_shadow_events_survive_the_durable_codec() -> None:
    runtime, store = _runtime()
    runtime.shadow = _shadow_advisor_for(runtime)
    state = runtime.run("probe the shadow")
    for event in store.events_for(state.run_id):
        assert CODEC.decode(CODEC.encode(event)) == event
