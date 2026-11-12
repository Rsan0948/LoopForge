import threading
from datetime import UTC, datetime

import pytest

from loopforge.adapters import memory
from loopforge.domain.events import Event, RunStarted
from loopforge.domain.state import RunRecord
from loopforge.domain.types import EventId, RunId


def test_memory_store_listing_is_a_snapshot_during_run_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = memory.InMemoryEventStore()

    def started(key: str) -> RunStarted:
        return RunStarted(
            event_id=EventId(f"event-{key}"),
            run_id=RunId(key),
            occurred_at=datetime.now(UTC),
            sequence=1,
            objective=f"Run {key}",
        )

    store.append(started("first"), expected_version=0)
    reached = threading.Event()
    written = threading.Event()
    original = memory.summarize_run

    def summarize(run_id: RunId, events: tuple[Event, ...]) -> RunRecord:
        reached.set()
        assert written.wait(5), "Writer did not reach the deterministic interleaving"
        return original(run_id, events)

    def writer() -> None:
        if reached.wait(5):
            store.append(started("second"), expected_version=0)
            written.set()

    monkeypatch.setattr(memory, "summarize_run", summarize)
    thread = threading.Thread(target=writer)
    thread.start()
    failure = None
    try:
        store.list_runs()
    except RuntimeError as exc:
        failure = str(exc)
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert failure is None, failure
