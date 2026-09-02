"""Fan-out event store: durable delegate plus per-run subscriber queues.

This is the D6 fan-out sink for the operator server: every event is pushed
to a run's subscriber queues ONLY after the delegate has durably appended it
— never before, and never when the append fails (a version conflict or
duplicate must not leak a phantom event to live subscribers). The wrapper
itself holds no run-state authority: reads and writes delegate verbatim to
the wrapped :class:`StateStorePort`, and the subscriber side-channel is a
best-effort projection consumers reconcile against the durable stream.

Thread safety: a single lock guards the subscriber registry; queues are
unbounded ``queue.Queue`` instances fed with ``put_nowait``, so an append
can never block on a slow consumer. Subscribing to a run with no stream is
allowed — events may arrive later.
"""

from __future__ import annotations

import queue
import threading

from loopforge.domain.events import Event
from loopforge.domain.state import RunRecord
from loopforge.domain.types import RunId
from loopforge.ports.state_store import StateStorePort


class FanOutEventStore:
    """A ``StateStorePort``-conforming wrapper that fans appended events out.

    Store semantics (CAS append, stream reads, runs index) are inherited from
    the delegate unchanged; the only added behavior is the post-append
    publish to that run's subscriber queues.
    """

    def __init__(self, delegate: StateStorePort) -> None:
        self._delegate = delegate
        self._lock = threading.Lock()
        self._subscribers: dict[RunId, set[queue.Queue[Event]]] = {}

    def append(self, event: Event, *, expected_version: int) -> int:
        """Durably append via the delegate, then publish to subscribers.

        The publish happens strictly after a successful append; a raising
        delegate delivers nothing.
        """
        version = self._delegate.append(event, expected_version=expected_version)
        with self._lock:
            queues = tuple(self._subscribers.get(event.run_id, ()))
        for subscriber in queues:
            subscriber.put_nowait(event)
        return version

    def events_for(self, run_id: RunId) -> tuple[Event, ...]:
        return self._delegate.events_for(run_id)

    def current_version(self, run_id: RunId) -> int:
        return self._delegate.current_version(run_id)

    def list_runs(self) -> tuple[RunRecord, ...]:
        return self._delegate.list_runs()

    def subscribe(self, run_id: RunId) -> queue.Queue[Event]:
        """Register and return a new unbounded subscriber queue for a run.

        Only events appended AFTER this call are queued; callers replay
        history from the durable stream and skip sequences they have seen.
        """
        subscriber: queue.Queue[Event] = queue.Queue()
        with self._lock:
            self._subscribers.setdefault(run_id, set()).add(subscriber)
        return subscriber

    def unsubscribe(self, run_id: RunId, subscriber: queue.Queue[Event]) -> None:
        """Remove a subscriber queue; unknown queues are ignored."""
        with self._lock:
            queues = self._subscribers.get(run_id)
            if queues is None:
                return
            queues.discard(subscriber)
            if not queues:
                del self._subscribers[run_id]
