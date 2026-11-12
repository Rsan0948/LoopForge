"""Admission diagnostics are durable evidence and never provider authority."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
)
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.admission import ContextAdmission, InputMeasurementBasis
from loopforge.domain.context import ModelContext
from loopforge.domain.events import ModelAttemptFailed, ModelTurnRecorded
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, BudgetLimit, Permission, RiskLevel
from loopforge.ports.model import ModelFailureClass, ModelTurn, ModelTurnError
from loopforge.ports.tools import ToolResult

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def admission() -> ContextAdmission:
    return ContextAdmission(
        measurement_basis=InputMeasurementBasis.UTF8_BYTE_UPPER_BOUND,
        input_token_bound=1234,
        serialized_request_bytes=1234,
        context_window_tokens=32768,
        output_reserve_tokens=8192,
        framing_reserve_tokens=1024,
        compacted_turns=2,
    )


@pytest.mark.parametrize("denied", [True, False])
def test_runtime_persists_admission_for_successful_and_denied_requests(denied: bool) -> None:
    measured = replace(admission(), input_token_bound=40000) if denied else admission()

    class MeasuredModel(ScriptedModel):
        def propose_action(self, context: ModelContext) -> ModelTurn:
            if denied:
                raise ModelTurnError(
                    ModelFailureClass.PERMANENT,
                    "MODEL_CONTEXT_LIMIT",
                    "immutable envelope exceeded",
                    context_admission=measured,
                )
            return replace(super().propose_action(context), context_admission=measured)

    store = InMemoryEventStore()
    clock = FixedClock(NOW)
    runtime = Runtime(
        model=MeasuredModel([ActionProposal(ActionId("inspect"), "read", {})]),
        tools=ScriptedTools(
            [ToolResult(ok=True, observation="verified")],
            metadata=[
                ToolMetadata(
                    name="read",
                    risk=RiskLevel.READ_ONLY,
                    required_permission=Permission.READ,
                    side_effect=SideEffectClass.READ_ONLY,
                    retry=RetryClass.SAFE,
                    idempotency=IdempotencyClass.NATURAL,
                    approval=ApprovalClass.NONE,
                    timeout_seconds=1,
                )
            ],
        ),
        verifier=ObservationContainsVerifier("verified"),
        store=store,
        control=ControlPolicy(BudgetLimit(max_cost_usd=1, max_iterations=3)),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(),
        context=BasicContextBuilder(clock),
        clock=clock,
        sleeper=RecordingSleeper(),
        verify_read_only_turns=True,
    )
    result = runtime.run("Inspect within the immutable envelope")
    event = next(
        e
        for e in store.events_for(result.run_id)
        if isinstance(e, ModelTurnRecorded | ModelAttemptFailed)
    )
    assert event.context_admission == measured
    codec = JsonEventCodec()
    assert codec.decode(codec.encode(event)) == event
    payload = json.loads(codec.encode(event))
    payload["event"]["context_admission"]["grant_network"] = True
    with pytest.raises(ValueError, match="admission fields"):
        codec.decode(json.dumps(payload))
    del payload["event"]["context_admission"]
    assert codec.decode(json.dumps(payload)) == replace(event, context_admission=None)


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "120", None])
def test_admission_counts_reject_coercion(invalid: object) -> None:
    with pytest.raises(ValueError, match="nonnegative integers"):
        replace(admission(), input_token_bound=invalid)


def test_measurement_basis_and_window_are_code_owned() -> None:
    with pytest.raises(TypeError, match="code-owned"):
        replace(admission(), measurement_basis="token_counter")
    with pytest.raises(ValueError, match="positive"):
        replace(admission(), context_window_tokens=0)
    assert not replace(admission(), output_reserve_tokens=40000).admitted
