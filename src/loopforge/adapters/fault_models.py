"""Fault-injection model adapters (PACS-016, M5): code-owned, deterministic.

``ModelPort`` decorators that inject provider faults for the evaluation
laboratory. Fault injection is CODE-OWNED EVAL CONFIGURATION: an operator
wires a decorator around a model when a benchmark binding declares a fault
descriptor, and model output can never request, shape, or disable an
injection (AGENTS.md rules 4, 12, 14). Determinism is total — failures are
counted, never random — so the same decorator over the same wrapped model
reproduces the same turn sequence on every run.

Both decorators normalize the injected failure into exactly the runtime's
transient model-turn vocabulary — ``ModelTurnError`` with
``ModelFailureClass.TRANSIENT`` and the provider-independent
``MODEL_UNAVAILABLE`` reason code, mirroring how ``OllamaModel`` normalizes
an unavailable provider — so the runtime's bounded retry streak (and only
it) decides whether the run recovers or stops ``FAILURE`` with a durable
``MODEL_*`` reason. The decorators hold no stopping or retry policy of
their own; they report an operational fact and the runtime derives the
consequence from the code-owned reliability policy.

These adapters exist for the evaluation laboratory: deterministic suites
wrap ``ScriptedModel`` (the two fault-injection benchmark categories are
never live-eligible), and a live evaluation may optionally wrap the live
adapter to measure recovery behavior against a real provider.
"""

from __future__ import annotations

from typing import Final

from loopforge.domain.context import ModelContext
from loopforge.domain.routing import ModelCapabilities
from loopforge.ports.model import (
    ModelFailureClass,
    ModelPort,
    ModelTurn,
    ModelTurnError,
)

_INJECTED_REASON_CODE: Final = "MODEL_UNAVAILABLE"
_INJECTED_SUMMARY: Final = "injected provider fault (fault-injection model adapter)"


def _injected_transient_error() -> ModelTurnError:
    """The exact transient error shape the runtime's bounded streak handles.

    Same class, same reason code, same provider-independent vocabulary a
    live adapter raises for an unavailable provider — the runtime cannot
    (and must not) distinguish an injected fault from a real one.
    """
    return ModelTurnError(ModelFailureClass.TRANSIENT, _INJECTED_REASON_CODE, _INJECTED_SUMMARY)


def _validate_wrapped(wrapped: ModelPort) -> None:
    """Fail closed at wiring time when the wrapped object is not model-shaped."""
    if not callable(getattr(wrapped, "propose_action", None)):
        msg = "fault-injection decorators must wrap a model implementing propose_action"
        raise TypeError(msg)


class TransientFailureModel:
    """Fail the first ``transient_failures`` propose calls, then delegate.

    Each failed call raises the runtime's transient model-turn error, so the
    failures burn the bounded retry streak without burning the iteration
    budget; once the counted failures are exhausted every call delegates to
    the wrapped model unchanged. ``transient_failures=0`` is a pure
    passthrough. One instance is single-threaded, matching the runtime's
    synchronous control loop and the wrapped adapters' own contract.
    """

    def __init__(self, wrapped: ModelPort, transient_failures: int) -> None:
        _validate_wrapped(wrapped)
        if isinstance(transient_failures, bool) or not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            transient_failures, int
        ):
            msg = "transient_failures must be an integer"
            raise ValueError(msg)  # noqa: TRY004
        if transient_failures < 0:
            msg_2 = "transient_failures cannot be negative"
            raise ValueError(msg_2)
        self._wrapped = wrapped
        self._remaining = transient_failures

    @property
    def capabilities(self) -> ModelCapabilities:
        """Honest delegation: the wrapped model's capabilities are the truth."""
        return self._wrapped.capabilities

    def propose_action(self, context: ModelContext) -> ModelTurn:
        if self._remaining > 0:
            self._remaining -= 1
            raise _injected_transient_error()
        return self._wrapped.propose_action(context)

    def close(self) -> None:
        """Delegate lifecycle honestly to the wrapped model.

        Mirrors ``RepairRuntimeBundle.close``: a wrapped model without a
        ``close()`` hook (e.g. ``ScriptedModel``) makes this a no-op; the
        decorator itself holds no resources.
        """
        close = getattr(self._wrapped, "close", None)
        if callable(close):
            close()


class OutageModel:
    """Fail every propose call: a provider outage without recovery.

    Every call raises the runtime's transient model-turn error, so the
    outage always outlasts the bounded retry streak: the runtime stops the
    run ``FAILURE`` with the durable ``MODEL_UNAVAILABLE`` reason once the
    streak reaches the reliability policy's max attempts. The wrapped model
    is never asked for a turn — an outage means the provider cannot be
    reached at all.
    """

    def __init__(self, wrapped: ModelPort) -> None:
        _validate_wrapped(wrapped)
        self._wrapped = wrapped

    @property
    def capabilities(self) -> ModelCapabilities:
        """Honest delegation: the wrapped model's capabilities are the truth."""
        return self._wrapped.capabilities

    def propose_action(self, context: ModelContext) -> ModelTurn:
        del context  # an outage never reaches the provider
        raise _injected_transient_error()

    def close(self) -> None:
        """Delegate lifecycle honestly to the wrapped model (no-op without close())."""
        close = getattr(self._wrapped, "close", None)
        if callable(close):
            close()
