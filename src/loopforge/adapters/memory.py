from __future__ import annotations

from collections import defaultdict

from loopforge.domain.events import Event
from loopforge.domain.state import RunRecord, summarize_run
from loopforge.domain.types import EventId, RunId
from loopforge.ports.state_store import DuplicateEventError, StreamVersionConflictError


class InMemoryEventStore:
    """Reference in-memory event store with the same CAS contract as SQLite."""

    def __init__(self) -> None:
        self._events: dict[RunId, list[Event]] = defaultdict(list)
        self._event_ids: set[EventId] = set()

    def append(self, event: Event, *, expected_version: int) -> int:
        actual = len(self._events[event.run_id])
        if actual != expected_version:
            raise StreamVersionConflictError(
                event.run_id,
                expected=expected_version,
                actual=actual,
            )
        if event.sequence != expected_version + 1:
            msg = (
                f"event sequence {event.sequence} does not match "
                f"expected next sequence {expected_version + 1}"
            )
            raise ValueError(msg)
        if event.event_id in self._event_ids:
            msg_2 = f"duplicate event id: {event.event_id}"
            raise DuplicateEventError(msg_2)
        self._events[event.run_id].append(event)
        self._event_ids.add(event.event_id)
        return expected_version + 1

    def events_for(self, run_id: RunId) -> tuple[Event, ...]:
        return tuple(self._events[run_id])

    def current_version(self, run_id: RunId) -> int:
        return len(self._events[run_id])

    def list_runs(self) -> tuple[RunRecord, ...]:
        records = [
            summarize_run(run_id, tuple(events))
            for run_id, events in self._events.items()
            if events
        ]
        records.sort(key=lambda record: record.started_at, reverse=True)
        return tuple(records)
