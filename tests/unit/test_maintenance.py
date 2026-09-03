"""Force-stop maintenance: replay-free terminal appends, fail-closed CAS.

Pins the PACS-015 escape hatch at the store layer: the stop record lands on
healthy and corrupted streams alike (the version counter is all it needs),
unknown runs are denied, and a racing append between the version read and
the compare-and-append turns into a loud conflict — never a reordered
stream.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import FixedClock
from loopforge.application.maintenance import force_stop_run
from loopforge.application.runtime import UnknownRunError
from loopforge.domain.events import RunStarted, RunStopped
from loopforge.domain.state import InvalidTransitionError, replay
from loopforge.domain.types import EventId, RunId, RunStatus, StopReason
from loopforge.ports.state_store import StreamVersionConflictError

NOW = datetime(2026, 9, 2, tzinfo=UTC)
RUN = RunId("maintenance-run")


def _run_started(sequence: int) -> RunStarted:
    return RunStarted(
        event_id=EventId(f"evt_{sequence:016d}"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        objective="repair the calculator",
    )


def _started_store() -> InMemoryEventStore:
    store = InMemoryEventStore()
    store.append(_run_started(1), expected_version=0)
    return store


def test_force_stop_appends_a_terminal_cancel_record_without_replay() -> None:
    store = _started_store()

    version = force_stop_run(store, RUN, summary="wedged driver", clock=FixedClock(NOW))

    assert version == 2
    stop = store.events_for(RUN)[-1]
    assert isinstance(stop, RunStopped)
    assert stop.reason is StopReason.CANCELLED
    assert stop.summary == "wedged driver"
    assert stop.sequence == 2
    state = replay(RUN, store.events_for(RUN))
    assert state.status is RunStatus.CANCELLED
    assert state.status.is_terminal


def test_force_stop_denies_an_unknown_run() -> None:
    store = InMemoryEventStore()

    with pytest.raises(UnknownRunError, match="no persisted run"):
        force_stop_run(store, RUN, summary="nope", clock=FixedClock(NOW))

    assert store.current_version(RUN) == 0


def test_force_stop_tolerates_a_stream_the_reducer_rejects() -> None:
    # A second RunStarted makes the stream unreplayable (RunStarted is legal
    # only from CREATED); the version counter is unaffected by the corruption.
    store = _started_store()
    store.append(_run_started(2), expected_version=1)
    with pytest.raises(InvalidTransitionError):
        replay(RUN, store.events_for(RUN))

    version = force_stop_run(store, RUN, summary="corrupted stream", clock=FixedClock(NOW))

    assert version == 3
    assert isinstance(store.events_for(RUN)[-1], RunStopped)


class _RacingStore(InMemoryEventStore):
    """Simulates a driver append landing between the version read and the CAS."""

    def __init__(self) -> None:
        super().__init__()
        self._triggered = False

    def current_version(self, run_id: RunId) -> int:
        version = super().current_version(run_id)
        if version == 1 and not self._triggered:
            self._triggered = True
            super().append(_run_started(2), expected_version=version)
        return version


def test_force_stop_fails_closed_against_a_racing_append() -> None:
    store = _RacingStore()
    store.append(_run_started(1), expected_version=0)

    with pytest.raises(StreamVersionConflictError):
        force_stop_run(store, RUN, summary="racing driver", clock=FixedClock(NOW))

    # The racing stream is untouched: no stop record was slipped in out of order.
    assert [event.sequence for event in store.events_for(RUN)] == [1, 2]
