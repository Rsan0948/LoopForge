from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.sqlite_events import (
    STORE_SCHEMA_VERSION,
    SQLiteEventStore,
    UnsupportedStoreSchemaError,
)
from loopforge.domain.events import PlanCreated, RunStarted
from loopforge.domain.state import replay
from loopforge.domain.types import EventId, RunId, RunStatus
from loopforge.ports.state_store import DuplicateEventError, StreamVersionConflictError

NOW = datetime(2026, 8, 22, tzinfo=UTC)
RUN = RunId("sqlite-run")


def _store(path: Path) -> SQLiteEventStore:
    return SQLiteEventStore(path, codec=JsonEventCodec())


def _started(*, event_id: str = "e1", sequence: int = 1, run_id: RunId = RUN) -> RunStarted:
    return RunStarted(
        event_id=EventId(event_id),
        run_id=run_id,
        occurred_at=NOW,
        sequence=sequence,
        objective="repair",
    )


def test_sqlite_store_initializes_versioned_schema_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    first = _store(path)
    second = _store(path)

    assert first.schema_version() == STORE_SCHEMA_VERSION
    assert second.schema_version() == STORE_SCHEMA_VERSION

    with closing(sqlite3.connect(path)) as connection:
        versions = connection.execute(
            "SELECT version FROM event_store_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert versions == [(1,)]
    assert {"events", "event_store_migrations"} <= tables


def test_sqlite_store_rejects_schema_newer_than_runtime(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE event_store_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        connection.execute(
            "INSERT INTO event_store_migrations(version, applied_at) VALUES (999, 'future')"
        )
        connection.commit()

    with pytest.raises(UnsupportedStoreSchemaError, match="newer than supported"):
        _store(path)


def test_sqlite_store_round_trips_events_and_replays_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    store = _store(path)
    started = _started()
    planned = PlanCreated(
        event_id=EventId("e2"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=2,
        caused_by=started.event_id,
        plan="inspect",
    )

    assert store.append(started, expected_version=0) == 1
    assert store.append(planned, expected_version=1) == 2

    reopened = _store(path)
    events = reopened.events_for(RUN)
    assert events == (started, planned)
    assert reopened.current_version(RUN) == 2
    state = replay(RUN, events)
    assert state.status is RunStatus.READY
    assert state.plan == "inspect"
    assert state.version == 2


def test_two_sqlite_writers_detect_stale_expected_version(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    writer_a = _store(path)
    writer_b = _store(path)

    assert writer_a.current_version(RUN) == 0
    assert writer_b.current_version(RUN) == 0
    writer_a.append(_started(event_id="a"), expected_version=0)

    stale = _started(event_id="b")
    with pytest.raises(StreamVersionConflictError) as exc_info:
        writer_b.append(stale, expected_version=0)

    assert exc_info.value.expected == 0
    assert exc_info.value.actual == 1
    assert writer_b.current_version(RUN) == 1


def test_sqlite_store_rejects_sequence_mismatch_before_insert(tmp_path: Path) -> None:
    store = _store(tmp_path / "events.db")
    with pytest.raises(ValueError, match="expected next sequence 1"):
        store.append(_started(sequence=2), expected_version=0)
    assert store.current_version(RUN) == 0


def test_sqlite_store_rejects_duplicate_event_id_across_streams(tmp_path: Path) -> None:
    store = _store(tmp_path / "events.db")
    store.append(_started(event_id="same"), expected_version=0)
    duplicate = _started(event_id="same", run_id=RunId("other"))

    with pytest.raises(DuplicateEventError, match="duplicate event id"):
        store.append(duplicate, expected_version=0)


def test_sqlite_events_table_rejects_update_and_delete(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    store = _store(path)
    store.append(_started(), expected_version=0)

    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE events SET event_type = 'Tampered' WHERE run_id = ?",
                (str(RUN),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events WHERE run_id = ?", (str(RUN),))

    assert store.current_version(RUN) == 1


def test_concurrent_sqlite_writers_allow_exactly_one_compare_and_append(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    _store(path)  # initialize schema before racing writers

    def write(event_id: str) -> str:
        store = _store(path)
        try:
            store.append(_started(event_id=event_id), expected_version=0)
        except StreamVersionConflictError:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, ["race-a", "race-b"]))

    assert sorted(outcomes) == ["committed", "conflict"]
    final = _store(path)
    assert final.current_version(RUN) == 1
    assert len(final.events_for(RUN)) == 1
