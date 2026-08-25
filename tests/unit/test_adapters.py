from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
)
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import RunStarted
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.state import RunState
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, EventId, Permission, RiskLevel, RunId
from loopforge.ports.state_store import DuplicateEventError, StreamVersionConflictError
from loopforge.ports.tools import ToolExecutionRequest, ToolResult, UnknownToolError

NOW = datetime(2026, 8, 22, tzinfo=UTC)
RUN = RunId("adapter-run")


def _started(*, event_id: str = "e1", sequence: int = 1, run_id: RunId = RUN) -> RunStarted:
    return RunStarted(
        event_id=EventId(event_id),
        run_id=run_id,
        occurred_at=NOW,
        sequence=sequence,
        objective="repair",
    )


def _metadata(name: str = "inspect") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _request(tool_name: str = "inspect") -> ToolExecutionRequest:
    return ToolExecutionRequest(
        proposal=ActionProposal(ActionId("a1"), tool_name, {}),
        attempt=1,
        timeout_seconds=5.0,
    )


# --- InMemoryEventStore -----------------------------------------------------


def test_memory_store_appends_and_returns_next_version() -> None:
    store = InMemoryEventStore()

    assert store.append(_started(), expected_version=0) == 1
    assert store.append(_started(event_id="e2", sequence=2), expected_version=1) == 2
    assert store.current_version(RUN) == 2


def test_memory_store_rejects_stale_expected_version_and_stays_unchanged() -> None:
    store = InMemoryEventStore()
    store.append(_started(), expected_version=0)

    with pytest.raises(StreamVersionConflictError) as exc_info:
        store.append(_started(event_id="e2", sequence=1), expected_version=0)

    assert exc_info.value.run_id == RUN
    assert exc_info.value.expected == 0
    assert exc_info.value.actual == 1
    assert store.current_version(RUN) == 1
    assert store.events_for(RUN) == (_started(),)


def test_memory_store_rejects_future_expected_version() -> None:
    store = InMemoryEventStore()

    with pytest.raises(StreamVersionConflictError, match="expected 5, actual 0"):
        store.append(_started(sequence=6), expected_version=5)

    assert store.current_version(RUN) == 0


def test_memory_store_rejects_sequence_mismatch_before_append() -> None:
    store = InMemoryEventStore()

    with pytest.raises(ValueError, match="expected next sequence 1"):
        store.append(_started(sequence=2), expected_version=0)

    assert store.current_version(RUN) == 0
    assert store.events_for(RUN) == ()


def test_memory_store_rejects_duplicate_event_id_after_correct_cas_checks() -> None:
    store = InMemoryEventStore()
    store.append(_started(event_id="same"), expected_version=0)

    with pytest.raises(DuplicateEventError, match="duplicate event id"):
        store.append(_started(event_id="same", sequence=2), expected_version=1)

    assert store.current_version(RUN) == 1


def test_memory_store_rejects_duplicate_event_id_across_streams() -> None:
    store = InMemoryEventStore()
    store.append(_started(event_id="same"), expected_version=0)
    duplicate = _started(event_id="same", run_id=RunId("other"))

    with pytest.raises(DuplicateEventError, match="duplicate event id"):
        store.append(duplicate, expected_version=0)

    assert store.current_version(RunId("other")) == 0


def test_memory_store_isolates_streams_per_run_id() -> None:
    store = InMemoryEventStore()
    other = RunId("other")

    assert store.events_for(RunId("missing")) == ()
    assert store.current_version(RunId("missing")) == 0

    store.append(_started(event_id="a"), expected_version=0)
    store.append(_started(event_id="b", run_id=other), expected_version=0)

    assert store.events_for(RUN) == (_started(event_id="a"),)
    assert store.events_for(other) == (_started(event_id="b", run_id=other),)
    assert store.current_version(RUN) == 1
    assert store.current_version(other) == 1


def test_memory_store_events_for_returns_immutable_tuple_in_append_order() -> None:
    store = InMemoryEventStore()
    first = _started(event_id="first")
    second = _started(event_id="second", sequence=2)
    store.append(first, expected_version=0)
    store.append(second, expected_version=1)

    events = store.events_for(RUN)

    assert isinstance(events, tuple)
    assert events == (first, second)


@given(ids=st.lists(st.uuids(), min_size=0, max_size=20, unique=True))
@example(ids=[])
@example(ids=[UUID(int=1)])
def test_memory_store_property_appends_preserve_order_and_versions(ids: list[UUID]) -> None:
    store = InMemoryEventStore()
    appended: list[RunStarted] = []

    for index, uid in enumerate(ids):
        event = _started(event_id=str(uid), sequence=index + 1)
        assert store.append(event, expected_version=index) == index + 1
        appended.append(event)
        assert store.current_version(RUN) == index + 1

    assert store.events_for(RUN) == tuple(appended)
    assert [event.sequence for event in store.events_for(RUN)] == list(range(1, len(ids) + 1))


@given(
    steps=st.integers(min_value=0, max_value=8),
    wrong=st.integers(min_value=0, max_value=12),
)
@example(steps=0, wrong=1)
def test_memory_store_property_any_wrong_expected_version_conflicts(steps: int, wrong: int) -> None:
    assume(wrong != steps)
    store = InMemoryEventStore()
    for index in range(steps):
        store.append(_started(event_id=f"e{index}", sequence=index + 1), expected_version=index)

    wrong_version = wrong
    with pytest.raises(StreamVersionConflictError) as exc_info:
        store.append(
            _started(event_id="stale", sequence=wrong_version + 1),
            expected_version=wrong_version,
        )

    assert exc_info.value.expected == wrong_version
    assert exc_info.value.actual == steps
    assert store.current_version(RUN) == steps
    assert len(store.events_for(RUN)) == steps


@given(
    steps=st.integers(min_value=0, max_value=8),
    sequence=st.integers(min_value=1, max_value=16),
)
@example(steps=0, sequence=2)
def test_memory_store_property_sequence_must_be_next(steps: int, sequence: int) -> None:
    store = InMemoryEventStore()
    for index in range(steps):
        store.append(_started(event_id=f"e{index}", sequence=index + 1), expected_version=index)

    if sequence == steps + 1:
        assert (
            store.append(_started(event_id="next", sequence=sequence), expected_version=steps)
            == steps + 1
        )
    else:
        with pytest.raises(ValueError, match="does not match expected next sequence"):
            store.append(_started(event_id="next", sequence=sequence), expected_version=steps)
        assert store.current_version(RUN) == steps


# --- SystemClock / SystemSleeper --------------------------------------------


def test_system_clock_returns_timezone_aware_utc_now() -> None:
    before = datetime.now(UTC)
    now = SystemClock().now()
    after = datetime.now(UTC)

    assert before <= now <= after
    assert now.tzinfo is UTC


def test_system_sleeper_forwards_duration_to_time_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", recorded.append)

    assert SystemSleeper().sleep(0.25) is None
    assert recorded == [0.25]


def test_system_sleeper_actually_blocks_for_requested_duration() -> None:
    started = time.monotonic()

    assert SystemSleeper().sleep(0.01) is None

    assert time.monotonic() - started >= 0.01


# --- FixedClock / RecordingSleeper -------------------------------------------


def test_fixed_clock_always_returns_the_same_instant() -> None:
    clock = FixedClock(NOW)

    assert clock.now() is NOW
    assert clock.now() is NOW


def test_recording_sleeper_records_delays_without_sleeping() -> None:
    sleeper = RecordingSleeper()

    assert sleeper.delays == []
    assert sleeper.sleep(0.5) is None
    assert sleeper.sleep(2.0) is None
    assert sleeper.delays == [0.5, 2.0]


# --- ScriptedModel ------------------------------------------------------------


def _state() -> RunState:
    return RunState(run_id=RUN)


def test_scripted_model_returns_scripted_actions_in_order_with_usage() -> None:
    first = ActionProposal(ActionId("a1"), "inspect", {})
    second = ActionProposal(ActionId("a2"), "inspect", {"path": "src"})
    model = ScriptedModel([first, second], cost_per_turn=0.5)

    turn_one = model.propose_action(_state())
    turn_two = model.propose_action(_state())

    assert turn_one.action is first
    assert turn_one.usage.cost_usd == 0.5
    assert turn_one.usage.input_tokens == 100
    assert turn_one.usage.output_tokens == 20
    assert turn_two.action is second


def test_scripted_model_default_cost_per_turn() -> None:
    model = ScriptedModel([ActionProposal(ActionId("a1"), "inspect", {})])

    assert model.propose_action(_state()).usage.cost_usd == 0.01


def test_scripted_model_exhaustion_raises_runtime_error() -> None:
    model = ScriptedModel([ActionProposal(ActionId("a1"), "inspect", {})])
    model.propose_action(_state())

    with pytest.raises(RuntimeError, match="scripted model exhausted"):
        model.propose_action(_state())

    with pytest.raises(RuntimeError, match="scripted model exhausted"):
        model.propose_action(_state())


@given(
    ids=st.lists(st.uuids(), min_size=0, max_size=10, unique=True),
    cost=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
)
@example(ids=[], cost=0.01)
def test_scripted_model_property_fifo_then_exhaustion(ids: list[UUID], cost: float) -> None:
    proposals = [ActionProposal(ActionId(str(uid)), "inspect", {}) for uid in ids]
    model = ScriptedModel(proposals, cost_per_turn=cost)

    for expected in proposals:
        turn = model.propose_action(_state())
        assert turn.action is expected
        assert turn.usage.cost_usd == cost

    with pytest.raises(RuntimeError, match="scripted model exhausted"):
        model.propose_action(_state())


# --- ScriptedTools ------------------------------------------------------------


def test_scripted_tools_returns_registered_metadata() -> None:
    inspect = _metadata()
    tools = ScriptedTools([ToolResult(ok=True, observation="ok")], metadata=[inspect])

    assert tools.metadata_for("inspect") is inspect


def test_scripted_tools_rejects_unknown_tool_metadata_lookup() -> None:
    tools = ScriptedTools([ToolResult(ok=True, observation="ok")], metadata=[_metadata()])

    with pytest.raises(UnknownToolError, match="unknown tool: missing") as exc_info:
        tools.metadata_for("missing")

    assert isinstance(exc_info.value.__cause__, KeyError)


def test_scripted_tools_executes_results_in_fifo_order() -> None:
    ok_result = ToolResult(ok=True, observation="fine")
    failed_result = ToolResult(
        ok=False,
        observation="boom",
        error_code="E_IO",
        failure_class=ToolFailureClass.TRANSIENT,
    )
    tools = ScriptedTools([ok_result, failed_result], metadata=[_metadata()])

    assert tools.execute(_request()) is ok_result
    assert tools.execute(_request()) is failed_result


def test_scripted_tools_execute_rejects_unregistered_tool_without_consuming_result() -> None:
    result = ToolResult(ok=True, observation="kept")
    tools = ScriptedTools([result], metadata=[_metadata()])

    with pytest.raises(UnknownToolError, match="unknown tool: missing"):
        tools.execute(_request(tool_name="missing"))

    # Defense-in-depth registration check runs before dequeuing a scripted result.
    assert tools.execute(_request()) is result


def test_scripted_tools_execute_exhaustion_raises_runtime_error() -> None:
    tools = ScriptedTools([ToolResult(ok=True, observation="done")], metadata=[_metadata()])
    tools.execute(_request())

    with pytest.raises(RuntimeError, match="scripted tool results exhausted"):
        tools.execute(_request())


# --- ObservationContainsVerifier ----------------------------------------------


def test_verifier_passes_when_observation_contains_expected() -> None:
    state = RunState(run_id=RUN, last_observation="all tests pass")

    result = ObservationContainsVerifier("tests pass").verify(state)

    assert result.passed is True
    assert result.summary == "expected 'tests pass'"
    assert result.score is None


def test_verifier_fails_when_observation_lacks_expected() -> None:
    state = RunState(run_id=RUN, last_observation="boom")

    result = ObservationContainsVerifier("tests pass").verify(state)

    assert result.passed is False
    assert result.summary == "expected 'tests pass'"


def test_verifier_fails_when_no_observation_recorded() -> None:
    result = ObservationContainsVerifier("tests pass").verify(RunState(run_id=RUN))

    assert result.passed is False


def test_verifier_with_empty_expected_passes_even_without_observation() -> None:
    result = ObservationContainsVerifier("").verify(RunState(run_id=RUN))

    assert result.passed is True


@given(expected=st.text(), observation=st.one_of(st.none(), st.text()))
@example(expected="needle", observation="a needle in a haystack")
@example(expected="needle", observation=None)
def test_verifier_property_matches_substring_membership(
    expected: str, observation: str | None
) -> None:
    result = ObservationContainsVerifier(expected).verify(
        RunState(run_id=RUN, last_observation=observation)
    )

    assert result.passed == (expected in (observation or ""))
    assert result.summary == f"expected {expected!r}"
