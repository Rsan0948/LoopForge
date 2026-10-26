"""Model-turn failure normalization through the runtime (deterministic).

A stub model raises normalized ``ModelTurnError``s exactly like a live adapter
would; the runtime owns the stopping/retry decisions. These tests pin the
failure seam without any provider I/O.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

import pytest

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedTools,
)
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext
from loopforge.domain.events import RunStopped
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy, RetrySettings
from loopforge.domain.routing import ModelCapabilities
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
from loopforge.ports.model import ModelFailureClass, ModelTurn, ModelTurnError
from loopforge.ports.tools import ToolResult

NOW = datetime(2026, 8, 28, tzinfo=UTC)


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


class FlakyModel:
    """Stub model: replays a scripted queue of normalized errors and actions."""

    def __init__(self, script: list[ModelTurnError | ActionProposal]) -> None:
        self._script = deque(script)
        self.calls = 0

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            provider="stub",
            model="flaky",
            supports_tool_calls=True,
            context_window_tokens=4096,
        )

    def propose_action(self, context: ModelContext) -> ModelTurn:
        del context
        self.calls += 1
        step = self._script.popleft()
        if isinstance(step, ModelTurnError):
            raise step
        return ModelTurn(
            action=step,
            usage=UsageDelta(cost_usd=0.01, input_tokens=10, output_tokens=5),
        )


def _runtime(
    model: FlakyModel,
    *,
    sleeper: RecordingSleeper,
    budget: BudgetLimit | None = None,
    store: InMemoryEventStore | None = None,
    observations: list[str] | None = None,
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
        control=ControlPolicy(budget or BudgetLimit(max_cost_usd=1.0, max_iterations=5)),
        permissions=PermissionPolicy(frozenset({Permission.READ, Permission.LOCAL_WRITE})),
        reliability=ReliabilityPolicy(retry=RetrySettings(max_attempts=3)),
        context=BasicContextBuilder(FixedClock(NOW)),
        clock=FixedClock(NOW),
        sleeper=sleeper,
        # Legacy cadence (PACS-016 M8): these pins exercise model-failure
        # handling, not cadence; success comes from a READ-class tool.
        verify_read_only_turns=True,
    )


def _transient(reason: str = "MODEL_UNAVAILABLE") -> ModelTurnError:
    return ModelTurnError(ModelFailureClass.TRANSIENT, reason, "provider unavailable (HTTP 503)")


def _stopped_summary(store: InMemoryEventStore, run_id: RunId) -> str:
    stopped = [event for event in store.events_for(run_id) if isinstance(event, RunStopped)]
    assert len(stopped) == 1
    return stopped[0].summary


# --- Permanent failures ---------------------------------------------------------


def test_permanent_model_failure_stops_run_explicitly() -> None:
    store = InMemoryEventStore()
    model = FlakyModel(
        [ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_INVALID_RESPONSE", "bad payload")],
    )
    runtime = _runtime(model, sleeper=RecordingSleeper(), store=store)
    state = runtime.run("objective")
    assert state.status is RunStatus.FAILED
    assert state.stop_reason is StopReason.FAILURE
    assert model.calls == 1
    summary = _stopped_summary(store, state.run_id)
    assert "MODEL_INVALID_RESPONSE" in summary
    assert "bad payload" in summary
    # Terminal states are durable and resume is a no-op.
    resumed = runtime.resume(state.run_id)
    assert resumed.status is RunStatus.FAILED
    assert model.calls == 1


@pytest.mark.parametrize(
    "failure_class", [ModelFailureClass.PERMANENT, ModelFailureClass.TRANSIENT]
)
def test_oversized_adapter_reason_code_still_terminates_the_run(
    failure_class: ModelFailureClass,
) -> None:
    store = InMemoryEventStore()
    code = "MODEL_" + "X" * 250
    errors: list[ModelTurnError | ActionProposal] = [
        ModelTurnError(failure_class, code, "adapter diagnostic") for _ in range(3)
    ]
    model = FlakyModel(errors)
    runtime = _runtime(model, sleeper=RecordingSleeper(), store=store)
    state = runtime.run("objective")
    assert state.status is RunStatus.FAILED
    stop = next(event for event in store.events_for(state.run_id) if isinstance(event, RunStopped))
    assert stop.reason_code == code[:200]
    assert runtime.resume(state.run_id) == state


# --- Transient failures -----------------------------------------------------------


def test_transient_failure_retries_with_backoff_then_proceeds() -> None:
    model = FlakyModel(
        [_transient(), ActionProposal(ActionId("a1"), "inspect", {"target": "auth"})],
    )
    sleeper = RecordingSleeper()
    runtime = _runtime(model, sleeper=sleeper)
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    assert model.calls == 2
    assert sleeper.delays == [0.25]


def test_transient_streak_exhaustion_stops_run() -> None:
    store = InMemoryEventStore()
    model = FlakyModel([_transient(), _transient(), _transient()])
    sleeper = RecordingSleeper()
    runtime = _runtime(model, sleeper=sleeper, store=store)
    state = runtime.run("objective")
    assert state.status is RunStatus.FAILED
    assert state.stop_reason is StopReason.FAILURE
    assert model.calls == 3
    assert sleeper.delays == [0.25, 0.5]
    assert "MODEL_UNAVAILABLE" in _stopped_summary(store, state.run_id)


def test_transient_timeout_reason_code_is_preserved_on_exhaustion() -> None:
    store = InMemoryEventStore()
    model = FlakyModel([_transient("MODEL_TIMEOUT")] * 3)
    runtime = _runtime(model, sleeper=RecordingSleeper(), store=store)
    state = runtime.run("objective")
    assert "MODEL_TIMEOUT" in _stopped_summary(store, state.run_id)


def test_transient_retries_do_not_burn_iteration_budget() -> None:
    model = FlakyModel(
        [
            _transient(),
            _transient(),
            ActionProposal(ActionId("a1"), "inspect", {"target": "auth"}),
        ],
    )
    runtime = _runtime(
        model,
        sleeper=RecordingSleeper(),
        budget=BudgetLimit(max_cost_usd=1.0, max_iterations=1),
    )
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    assert state.iteration == 1


def _invalid_response() -> ModelTurnError:
    return ModelTurnError(
        ModelFailureClass.TRANSIENT, "MODEL_INVALID_RESPONSE", "model proposed 0 tool actions"
    )


def test_invalid_model_response_is_reprompted_then_proceeds() -> None:
    # A single malformed turn (zero tool calls, bad JSON — a small-model
    # hiccup, not a run defect) is absorbed by the bounded reprompt.
    model = FlakyModel(
        [_invalid_response(), ActionProposal(ActionId("a1"), "inspect", {"target": "auth"})],
    )
    runtime = _runtime(model, sleeper=RecordingSleeper())
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    assert model.calls == 2


def test_invalid_response_streak_exhaustion_still_stops_the_run() -> None:
    store = InMemoryEventStore()
    model = FlakyModel([_invalid_response()] * 3)
    runtime = _runtime(model, sleeper=RecordingSleeper(), store=store)
    state = runtime.run("objective")
    assert state.status is RunStatus.FAILED
    assert state.stop_reason is StopReason.FAILURE
    assert model.calls == 3
    assert "MODEL_INVALID_RESPONSE" in _stopped_summary(store, state.run_id)


def test_failure_streak_resets_after_a_successful_turn() -> None:
    # Two transient failures, a successful (but unverified) action, then two
    # more transient failures: the streak must restart, not accumulate.
    model = FlakyModel(
        [
            _transient(),
            _transient(),
            ActionProposal(ActionId("a1"), "inspect", {"target": "auth"}),
            _transient(),
            _transient(),
            ActionProposal(ActionId("a2"), "inspect", {"target": "auth"}),
        ],
    )
    runtime = _runtime(
        model,
        sleeper=RecordingSleeper(),
        observations=["tests still failing", "all tests pass"],
    )
    state = runtime.run("objective")
    assert state.status is RunStatus.SUCCEEDED
    assert model.calls == 6


def test_runtime_rebuild_preserves_model_failure_allowance() -> None:
    store = InMemoryEventStore()
    first = _runtime(
        FlakyModel([_transient(), _transient()]), sleeper=RecordingSleeper(), store=store
    )
    run_id = first.start("objective")
    first.step(run_id)
    state = first.step(run_id)
    assert state.model_failure_streak == 2
    second = _runtime(FlakyModel([_transient()]), sleeper=RecordingSleeper(), store=store)
    assert second.step(run_id).status is RunStatus.FAILED
    assert second.state_for(run_id).model_failure_streak == 3


def test_rebuilt_adapter_cannot_reuse_an_action_identity() -> None:
    store = InMemoryEventStore()
    repeated = ActionProposal(ActionId("adapter-counter-1"), "inspect", {})
    first = _runtime(
        FlakyModel([repeated]), sleeper=RecordingSleeper(), store=store, observations=["not done"]
    )
    run_id = first.start("objective")
    first.step(run_id)
    second = _runtime(FlakyModel([repeated]), sleeper=RecordingSleeper(), store=store)
    second.resume(run_id)
    from loopforge.domain.events import ActionProposed  # noqa: PLC0415

    ids = [e.proposal.action_id for e in store.events_for(run_id) if isinstance(e, ActionProposed)]
    assert len(ids) == len(set(ids)) == 2


def test_verification_projection_only_consumes_committed_event() -> None:
    from loopforge.domain.events import VerificationPassed  # noqa: PLC0415
    from loopforge.domain.state import RunState  # noqa: PLC0415

    store = InMemoryEventStore()
    observed: list[int] = []

    class Observer:
        def verification_recorded(self, state: RunState) -> None:
            event = store.events_for(state.run_id)[-1]
            assert isinstance(event, VerificationPassed)
            assert event.sequence == state.version
            observed.append(state.version)

    runtime = _runtime(
        FlakyModel([ActionProposal(ActionId("local"), "inspect", {})]),
        sleeper=RecordingSleeper(),
        store=store,
    )
    runtime.verification_observer = Observer()
    assert runtime.run("objective").status is RunStatus.SUCCEEDED
    assert len(observed) == 1


def test_verification_projection_failure_stops_without_false_success() -> None:
    from loopforge.domain.state import RunState  # noqa: PLC0415

    class BrokenProjection:
        def verification_recorded(self, state: RunState) -> None:
            del state
            msg = "unavailable projection"
            raise OSError(msg)

    store = InMemoryEventStore()
    runtime = _runtime(
        FlakyModel([ActionProposal(ActionId("local"), "inspect", {})]),
        sleeper=RecordingSleeper(),
        store=store,
    )
    runtime.verification_observer = BrokenProjection()
    final = runtime.run("objective")
    assert final.status is RunStatus.FAILED
    stopped = [e for e in store.events_for(final.run_id) if isinstance(e, RunStopped)]
    assert stopped[-1].reason_code == "VERIFICATION_PROJECTION_FAILED"


def test_rebuild_notifies_committed_verification_before_next_model_call() -> None:
    from loopforge.domain.state import RunState  # noqa: PLC0415

    class Crash(BaseException):
        pass

    class CrashProjection:
        def verification_recorded(self, state: RunState) -> None:
            del state
            raise Crash

    store = InMemoryEventStore()
    action = ActionProposal(ActionId("counter"), "inspect", {})
    original = _runtime(
        FlakyModel([action]), sleeper=RecordingSleeper(), store=store, observations=["not repaired"]
    )
    original.verification_observer = CrashProjection()
    run_id = original.start("objective")
    with pytest.raises(Crash):
        original.step(run_id)
    committed = original.state_for(run_id)
    assert committed.status is RunStatus.REFLECTING
    model = FlakyModel([action])
    sequences: list[int | None] = []

    class Projection:
        def verification_recorded(self, state: RunState) -> None:
            if not sequences:
                assert model.calls == 0
                assert state.last_verification_sequence == committed.last_verification_sequence
            sequences.append(state.last_verification_sequence)

    rebuilt = _runtime(model, sleeper=RecordingSleeper(), store=store)
    rebuilt.verification_observer = Projection()
    assert rebuilt.resume(run_id).status is RunStatus.SUCCEEDED
    assert len(sequences) == 2


@pytest.mark.parametrize("failed", [False, True])
def test_capabilities_are_snapshotted_before_inference_and_not_read_again(failed: bool) -> None:
    class OneReadModel(FlakyModel):
        capability_reads = 0

        @property
        def capabilities(self) -> ModelCapabilities:
            self.capability_reads += 1
            assert self.capability_reads == 1
            return super().capabilities

    store = InMemoryEventStore()
    model = OneReadModel(
        [_transient()] if failed else [ActionProposal(ActionId("a"), "inspect", {})]
    )
    runtime = _runtime(model, sleeper=RecordingSleeper(), store=store)
    run_id = runtime.start("objective")
    runtime.step(run_id)
    assert model.capability_reads == 1
