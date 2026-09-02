"""Fan-out store: post-append publish semantics over a durable delegate.

The fan-out sink (D6) must deliver an event to subscribers ONLY after the
delegate durably appends it — a failed append (version conflict, duplicate)
delivers nothing — and unsubscribing must stop delivery.
"""

from __future__ import annotations

import queue
from datetime import UTC, datetime

import pytest

from loopforge.adapters.fanout_store import FanOutEventStore
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.domain.events import RunStarted
from loopforge.domain.types import EventId, RunId
from loopforge.ports.state_store import StreamVersionConflictError

NOW = datetime(2026, 9, 1, tzinfo=UTC)
RUN = RunId("run_fanout")
OTHER = RunId("run_other")


def _started(run_id: RunId = RUN, *, event_id: str, sequence: int) -> RunStarted:
    return RunStarted(
        event_id=EventId(event_id),
        run_id=run_id,
        occurred_at=NOW,
        sequence=sequence,
        objective="fan out",
    )


def test_append_delivers_to_subscribers_only_after_durable_append() -> None:
    store = FanOutEventStore(InMemoryEventStore())
    subscriber = store.subscribe(RUN)

    first = _started(event_id="e1", sequence=1)
    store.append(first, expected_version=0)

    assert subscriber.get_nowait() is first

    # A failed append (stale expected_version) must deliver nothing.
    with pytest.raises(StreamVersionConflictError):
        store.append(_started(event_id="e2", sequence=1), expected_version=0)
    with pytest.raises(queue.Empty):
        subscriber.get_nowait()


def test_fan_out_is_scoped_per_run() -> None:
    store = FanOutEventStore(InMemoryEventStore())
    run_subscriber = store.subscribe(RUN)
    other_subscriber = store.subscribe(OTHER)

    store.append(_started(event_id="e1", sequence=1), expected_version=0)

    assert run_subscriber.get_nowait().event_id == EventId("e1")
    with pytest.raises(queue.Empty):
        other_subscriber.get_nowait()


def test_unsubscribe_stops_delivery_and_is_idempotent() -> None:
    store = FanOutEventStore(InMemoryEventStore())
    subscriber = store.subscribe(RUN)
    store.append(_started(event_id="e1", sequence=1), expected_version=0)
    assert subscriber.get_nowait().event_id == EventId("e1")

    store.unsubscribe(RUN, subscriber)
    store.unsubscribe(RUN, subscriber)  # unknown queue: ignored
    store.unsubscribe(OTHER, subscriber)  # unknown run: ignored

    delegate_second = RunStarted(
        event_id=EventId("e3"),
        run_id=OTHER,
        occurred_at=NOW,
        sequence=1,
        objective="other",
    )
    store.append(delegate_second, expected_version=0)
    with pytest.raises(queue.Empty):
        subscriber.get_nowait()


def test_subscribing_to_a_run_with_no_stream_is_allowed() -> None:
    store = FanOutEventStore(InMemoryEventStore())
    subscriber = store.subscribe(RunId("run_future"))

    store.append(
        RunStarted(
            event_id=EventId("e9"),
            run_id=RunId("run_future"),
            occurred_at=NOW,
            sequence=1,
            objective="later",
        ),
        expected_version=0,
    )

    assert subscriber.get_nowait().event_id == EventId("e9")


def test_unsubscribing_one_subscriber_leaves_the_others() -> None:
    store = FanOutEventStore(InMemoryEventStore())
    first = store.subscribe(RUN)
    second = store.subscribe(RUN)

    store.unsubscribe(RUN, first)
    store.append(_started(event_id="e1", sequence=1), expected_version=0)

    with pytest.raises(queue.Empty):
        first.get_nowait()
    assert second.get_nowait().event_id == EventId("e1")


def test_reads_and_listing_delegate_verbatim() -> None:
    delegate = InMemoryEventStore()
    store = FanOutEventStore(delegate)
    store.append(_started(event_id="e1", sequence=1), expected_version=0)

    assert store.current_version(RUN) == delegate.current_version(RUN) == 1
    assert store.events_for(RUN) == delegate.events_for(RUN)
    assert [record.run_id for record in store.list_runs()] == [RUN]
    assert store.current_version(OTHER) == 0
