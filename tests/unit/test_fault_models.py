"""Unit pins for the M5 fault-injection model adapters (PACS-016).

Allow + deny coverage (AGENTS.md rule 10): negative/ill-typed failure counts
are rejected; the injected failure is EXACTLY the runtime's transient class
with the provider-independent ``MODEL_UNAVAILABLE`` reason code; passthrough
after exhaustion preserves responses and usage untouched; capabilities and
close() delegate honestly; behavior is deterministic — counted failures only,
no randomness.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from loopforge.adapters.fault_models import OutageModel, TransientFailureModel
from loopforge.adapters.scripted import ScriptedModel
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.types import ActionId, RunId, UsageDelta
from loopforge.ports.model import (
    ModelFailureClass,
    ModelPort,
    ModelTurn,
    ModelTurnError,
)

_RUN_ID = RunId("run_fault_models")


def _context() -> ModelContext:
    return ModelContext(
        run_id=_RUN_ID,
        items=(),
        assembled_at=datetime(2026, 9, 3, tzinfo=UTC),
    )


def _turn(action_id: str = "a1", *, cost: float = 0.01) -> ModelTurn:
    return ModelTurn(
        action=ActionProposal(ActionId(action_id), "inspect", {"target": "auth"}),
        usage=UsageDelta(cost_usd=cost, input_tokens=100, output_tokens=20),
    )


class _RecordingModel:
    """Minimal honest model: canned turns, recorded calls, closeable."""

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = list(turns)
        self.calls = 0
        self.closed = False
        self._capabilities = ModelCapabilities(
            provider="recording",
            model="recording-1",
            supports_tool_calls=True,
            context_window_tokens=4096,
        )

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def propose_action(self, context: ModelContext) -> ModelTurn:
        del context
        self.calls += 1
        return self._turns[self.calls - 1]

    def close(self) -> None:
        self.closed = True


def _scripted(*action_ids: str) -> ScriptedModel:
    return ScriptedModel(
        [
            ActionProposal(ActionId(action_id), "inspect", {"target": "auth"})
            for action_id in action_ids
        ]
    )


# --- Construction validation (deny) ------------------------------------------


@pytest.mark.parametrize("bad", [-1, -100])
def test_transient_failure_model_rejects_negative_counts(bad: int) -> None:
    with pytest.raises(ValueError, match="transient_failures cannot be negative"):
        TransientFailureModel(_scripted("a1"), bad)


@pytest.mark.parametrize("bad", [True, 1.5, "2"])
def test_transient_failure_model_rejects_non_integer_counts(bad: object) -> None:
    with pytest.raises(ValueError, match="transient_failures must be an integer"):
        TransientFailureModel(_scripted("a1"), bad)  # type: ignore[arg-type]


def _transient_over_non_model() -> None:
    TransientFailureModel(object(), 1)  # type: ignore[arg-type]


def _outage_over_non_model() -> None:
    OutageModel(object())  # type: ignore[arg-type]


@pytest.mark.parametrize("factory", [_transient_over_non_model, _outage_over_non_model])
def test_decorators_reject_non_model_wrapped(factory: Callable[[], None]) -> None:
    with pytest.raises(TypeError, match="must wrap a model implementing propose_action"):
        factory()


# --- Injected failure shape ---------------------------------------------------


def test_transient_failure_is_exactly_the_runtimes_transient_class() -> None:
    model = TransientFailureModel(_scripted("a1"), 1)
    with pytest.raises(ModelTurnError) as caught:
        model.propose_action(_context())
    error = caught.value
    assert error.failure_class is ModelFailureClass.TRANSIENT
    assert error.reason_code == "MODEL_UNAVAILABLE"
    assert error.reason_code.startswith("MODEL_")


def test_outage_failure_is_exactly_the_runtimes_transient_class() -> None:
    model = OutageModel(_scripted("a1"))
    with pytest.raises(ModelTurnError) as caught:
        model.propose_action(_context())
    error = caught.value
    assert error.failure_class is ModelFailureClass.TRANSIENT
    assert error.reason_code == "MODEL_UNAVAILABLE"


def test_outage_model_fails_every_call_and_never_delegates() -> None:
    wrapped = _RecordingModel([_turn()])
    model = OutageModel(wrapped)
    for _ in range(5):
        with pytest.raises(ModelTurnError):
            model.propose_action(_context())
    assert wrapped.calls == 0


# --- Passthrough behavior (allow) ---------------------------------------------


def test_zero_failures_is_a_pure_passthrough() -> None:
    model = TransientFailureModel(_scripted("a1", "a2"), 0)
    first = model.propose_action(_context())
    second = model.propose_action(_context())
    assert first.action.action_id == ActionId("a1")
    assert second.action.action_id == ActionId("a2")


def test_passthrough_after_exhaustion_preserves_responses_and_usage() -> None:
    model = TransientFailureModel(_scripted("a1", "a2"), 2)
    for _ in range(2):
        with pytest.raises(ModelTurnError):
            model.propose_action(_context())
    first = model.propose_action(_context())
    second = model.propose_action(_context())
    assert [first.action.action_id, second.action.action_id] == [ActionId("a1"), ActionId("a2")]
    # ScriptedModel's declared usage reaches the runtime untouched.
    assert first.usage == UsageDelta(cost_usd=0.01, input_tokens=100, output_tokens=20)
    assert second.usage == UsageDelta(cost_usd=0.01, input_tokens=100, output_tokens=20)


# --- Honest delegation ---------------------------------------------------------


def _transient_decorator(wrapped: ModelPort) -> TransientFailureModel:
    return TransientFailureModel(wrapped, 1)


def _outage_decorator(wrapped: ModelPort) -> OutageModel:
    return OutageModel(wrapped)


_DECORATORS = [_transient_decorator, _outage_decorator]


@pytest.mark.parametrize("decorate", _DECORATORS)
def test_capabilities_are_the_wrapped_models_truth(
    decorate: Callable[[ModelPort], TransientFailureModel | OutageModel],
) -> None:
    wrapped = _RecordingModel([_turn()])
    decorated = decorate(wrapped)
    assert decorated.capabilities is wrapped.capabilities


@pytest.mark.parametrize("decorate", _DECORATORS)
def test_close_delegates_to_the_wrapped_model(
    decorate: Callable[[ModelPort], TransientFailureModel | OutageModel],
) -> None:
    wrapped = _RecordingModel([_turn()])
    decorate(wrapped).close()
    assert wrapped.closed is True


@pytest.mark.parametrize("decorate", _DECORATORS)
def test_close_is_a_noop_when_the_wrapped_model_has_no_close(
    decorate: Callable[[ModelPort], TransientFailureModel | OutageModel],
) -> None:
    # Must not raise: ScriptedModel has no close() hook.
    decorate(_scripted("a1")).close()


# --- Determinism ----------------------------------------------------------------


def test_transient_failure_sequence_is_deterministic() -> None:
    def sequence() -> list[str]:
        model = TransientFailureModel(_scripted("a1", "a2"), 2)
        outcomes: list[str] = []
        for _ in range(4):
            try:
                outcomes.append(f"turn:{model.propose_action(_context()).action.action_id}")
            except ModelTurnError as error:
                outcomes.append(f"error:{error.reason_code}")
        return outcomes

    expected = ["error:MODEL_UNAVAILABLE", "error:MODEL_UNAVAILABLE", "turn:a1", "turn:a2"]
    assert sequence() == expected
    assert sequence() == expected
