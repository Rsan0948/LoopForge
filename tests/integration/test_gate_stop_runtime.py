"""A workload gate ends a run with its own code-owned reason, never as success."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import RecordingSleeper
from loopforge.domain.context import ModelContext
from loopforge.domain.events import ModelAttemptFailed, RunStopped
from loopforge.domain.types import RunStatus, StopReason
from loopforge.ports.model import ModelTurn, RunGateStop
from tests.integration.test_model_failure_runtime import (
    FlakyModel,
    _runtime,  # pyright: ignore[reportPrivateUsage]
    _stopped_summary,  # pyright: ignore[reportPrivateUsage]
)


class GatedModel(FlakyModel):
    def __init__(self, gate: RunGateStop) -> None:
        super().__init__([])
        self.gate = gate

    def propose_action(self, context: ModelContext) -> ModelTurn:
        del context
        self.calls += 1
        raise self.gate


@pytest.mark.parametrize(
    ("reason", "status"),
    [(StopReason.STALLED, RunStatus.STALLED), (StopReason.FAILURE, RunStatus.FAILED)],
)
def test_a_gate_stop_is_recorded_with_its_reason_not_as_a_model_failure(
    reason: StopReason, status: RunStatus
) -> None:
    store = InMemoryEventStore()
    model = GatedModel(RunGateStop(reason, "WORKLOAD_GATE", "no new information\nforged line"))
    state = _runtime(model, sleeper=RecordingSleeper(), store=store).run("objective")
    assert state.status is status
    assert state.stop_reason is reason
    assert model.calls == 1
    summary = _stopped_summary(store, state.run_id)
    assert summary.startswith("WORKLOAD_GATE: no new information")
    assert "\n" not in summary
    assert not any(isinstance(e, ModelAttemptFailed) for e in store.events_for(state.run_id))
    stopped = next(e for e in store.events_for(state.run_id) if isinstance(e, RunStopped))
    assert stopped.reason_code == "WORKLOAD_GATE"
    assert JsonEventCodec().decode(JsonEventCodec().encode(stopped)) == stopped
    legacy = json.loads(JsonEventCodec().encode(stopped))
    del legacy["event"]["reason_code"]
    decoded = JsonEventCodec().decode(json.dumps(legacy))
    assert isinstance(decoded, RunStopped)
    assert decoded.reason_code == ""
    for invalid in ("x" * 201, None, 7):
        with pytest.raises(ValueError, match="stop reason code"):
            replace(stopped, reason_code=invalid)


@pytest.mark.parametrize(
    "reason",
    [StopReason.SUCCESS_VERIFIED, StopReason.BUDGET_EXHAUSTED, StopReason.CANCELLED],
)
def test_a_gate_cannot_claim_success_or_another_authority(reason: StopReason) -> None:
    with pytest.raises(ValueError, match="stalled or failed"):
        RunGateStop(reason, "WORKLOAD_GATE", "x")


def test_a_gate_stop_needs_a_reason_code() -> None:
    with pytest.raises(ValueError, match="reason_code"):
        RunGateStop(StopReason.STALLED, " ", "x")
