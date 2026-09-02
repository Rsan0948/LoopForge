"""Postgres event store conformance against the SQLite baseline (D3/D4).

Ports the SQLite store suite as the conformance baseline: versioned schema,
compare-and-append conflict/duplicate parity, append-only trigger enforcement,
concurrent-writer CAS, and the runs-index projection. Gated on a reachable
Postgres (docker compose up -d loopforge-db); deterministic CI skips with a
reason code when no database is available.

The suite runs against a DEDICATED test database (default ``loopforge_test``,
created on demand) and fails closed on any DSN whose database name does not
end in ``_test``: the schema reset below (DROP SCHEMA public CASCADE)
annihilates whatever database it points at — run against the live server's
database it wiped real sessions mid-run (PACS-014b finding 5).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import psycopg
import psycopg.errors
import pytest
from psycopg import conninfo, sql

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.postgres_events import (
    STORE_SCHEMA_VERSION,
    PostgresEventStore,
    UnsupportedStoreSchemaError,
)
from loopforge.domain.events import BudgetDebited, PlanCreated, RunStarted, RunStopped
from loopforge.domain.state import replay, summarize_run
from loopforge.domain.types import EventId, RunId, RunStatus, StopReason, UsageDelta
from loopforge.ports.state_store import DuplicateEventError, StreamVersionConflictError

NOW = datetime(2026, 9, 1, tzinfo=UTC)
RUN = RunId("pg-run")
DSN = os.environ.get(
    "LOOPFORGE_TEST_POSTGRES_DSN",
    "postgresql://loopforge:loopforge@127.0.0.1:5432/loopforge_test",
)
_MAINTENANCE_DATABASE = "postgres"


def _maintenance_dsn(dsn: str) -> str:
    """DSN of the server's maintenance database (always present on Postgres)."""
    return conninfo.make_conninfo(dsn, dbname=_MAINTENANCE_DATABASE)


def _test_database_name(dsn: str) -> str:
    """Fail closed unless the DSN targets a dedicated test database.

    The suite resets the public schema (DROP SCHEMA public CASCADE); pointed
    at a database a live server uses, that reset annihilates live sessions
    (PACS-014b finding 5). Only database names ending in ``_test`` are
    accepted — a mistargeted DSN is a loud error, never a silent wipe.
    """
    raw_dbname = conninfo.conninfo_to_dict(dsn).get("dbname")
    dbname = raw_dbname if isinstance(raw_dbname, str) else ""
    if not dbname.endswith("_test"):
        msg = (
            "PG integration suite requires a dedicated test database (dbname "
            f"ending in '_test'), got {dbname!r}; refusing to run destructive "
            "schema resets against a database a live server may be using"
        )
        raise RuntimeError(msg)
    return dbname


def _postgres_available() -> bool:
    try:
        with psycopg.connect(_maintenance_dsn(DSN), connect_timeout=2):
            return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason=(
        "postgres unavailable; start the local store with "
        "`docker compose up -d loopforge-db` (or set LOOPFORGE_TEST_POSTGRES_DSN)"
    ),
)


def _started(*, event_id: str = "e1", sequence: int = 1, run_id: RunId = RUN) -> RunStarted:
    return RunStarted(
        event_id=EventId(event_id),
        run_id=run_id,
        occurred_at=NOW,
        sequence=sequence,
        objective="repair",
    )


@pytest.fixture(scope="session", autouse=True)
def test_database() -> str:
    """Create the dedicated test database if absent; fail closed on live DSNs."""
    dbname = _test_database_name(DSN)
    with psycopg.connect(_maintenance_dsn(DSN), autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if exists is None:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    return dbname


@pytest.fixture(scope="module")
def fresh_schema(test_database: str) -> None:
    """Reset the public schema once per module so migrations execute under test."""
    del test_database  # ensures the dedicated database exists before any reset
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")


@pytest.fixture
def store(fresh_schema: None) -> PostgresEventStore:
    PostgresEventStore(DSN, codec=JsonEventCodec())  # ensure schema before truncate
    with psycopg.connect(DSN) as connection:
        connection.execute("TRUNCATE events, runs")
    return PostgresEventStore(DSN, codec=JsonEventCodec())


def test_empty_dsn_is_rejected() -> None:
    with pytest.raises(ValueError, match="dsn cannot be empty"):
        PostgresEventStore("  ", codec=JsonEventCodec())


# --- dedicated-test-database guard (PACS-014b finding 5) ------------------------


def test_test_database_guard_allows_dedicated_test_dsn() -> None:
    assert _test_database_name("postgresql://u:p@db:5432/loopforge_test") == "loopforge_test"


def test_test_database_guard_refuses_a_production_database() -> None:
    with pytest.raises(RuntimeError, match="dedicated test database"):
        _test_database_name("postgresql://loopforge:loopforge@127.0.0.1:5432/loopforge")


def test_suite_executes_against_the_dedicated_test_database(store: PostgresEventStore) -> None:
    del store
    with psycopg.connect(DSN) as connection:
        row = connection.execute("SELECT current_database()").fetchone()
    assert row is not None
    assert str(row[0]).endswith("_test")


def test_store_initializes_versioned_schema_and_is_idempotent(store: PostgresEventStore) -> None:
    first = store
    second = PostgresEventStore(DSN, codec=JsonEventCodec())

    assert first.schema_version() == STORE_SCHEMA_VERSION
    assert second.schema_version() == STORE_SCHEMA_VERSION
    with psycopg.connect(DSN) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
    assert {"events", "event_store_migrations", "runs"} <= tables


def test_store_rejects_schema_newer_than_runtime(store: PostgresEventStore) -> None:
    del store
    with psycopg.connect(DSN) as connection:
        connection.execute("INSERT INTO event_store_migrations(version) VALUES (999)")
    try:
        with pytest.raises(UnsupportedStoreSchemaError, match="newer than supported"):
            PostgresEventStore(DSN, codec=JsonEventCodec())
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute("DELETE FROM event_store_migrations WHERE version = 999")


def test_store_round_trips_events_and_replays(store: PostgresEventStore) -> None:
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

    reopened = PostgresEventStore(DSN, codec=JsonEventCodec())
    events = reopened.events_for(RUN)
    assert events == (started, planned)
    assert reopened.current_version(RUN) == 2
    state = replay(RUN, events)
    assert state.status is RunStatus.READY
    assert state.plan == "inspect"


def test_two_writers_detect_stale_expected_version(store: PostgresEventStore) -> None:
    writer_a = store
    writer_b = PostgresEventStore(DSN, codec=JsonEventCodec())

    writer_a.append(_started(event_id="a"), expected_version=0)

    with pytest.raises(StreamVersionConflictError) as exc_info:
        writer_b.append(_started(event_id="b"), expected_version=0)

    assert exc_info.value.expected == 0
    assert exc_info.value.actual == 1
    assert writer_b.current_version(RUN) == 1


def test_store_rejects_sequence_mismatch_before_insert(store: PostgresEventStore) -> None:
    with pytest.raises(ValueError, match="expected next sequence 1"):
        store.append(_started(sequence=2), expected_version=0)
    assert store.current_version(RUN) == 0


def test_store_rejects_duplicate_event_id_across_streams(store: PostgresEventStore) -> None:
    store.append(_started(event_id="same"), expected_version=0)
    duplicate = _started(event_id="same", run_id=RunId("other"))

    with pytest.raises(DuplicateEventError, match="duplicate event id"):
        store.append(duplicate, expected_version=0)


def test_events_table_rejects_update_and_delete(store: PostgresEventStore) -> None:
    store.append(_started(), expected_version=0)

    with (
        psycopg.connect(DSN) as connection,
        pytest.raises(psycopg.errors.RaiseException, match="append-only"),
    ):
        connection.execute("UPDATE events SET event_type = 'Tampered' WHERE run_id = 'pg-run'")
    with (
        psycopg.connect(DSN) as connection,
        pytest.raises(psycopg.errors.RaiseException, match="append-only"),
    ):
        connection.execute("DELETE FROM events WHERE run_id = 'pg-run'")

    assert store.current_version(RUN) == 1


def test_concurrent_writers_allow_exactly_one_compare_and_append(
    store: PostgresEventStore,
) -> None:
    del store  # schema initialized by the fixture; writers race below

    def write(event_id: str) -> str:
        writer = PostgresEventStore(DSN, codec=JsonEventCodec())
        try:
            writer.append(_started(event_id=event_id), expected_version=0)
        except StreamVersionConflictError:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, ["race-a", "race-b"]))

    assert sorted(outcomes) == ["committed", "conflict"]
    final = PostgresEventStore(DSN, codec=JsonEventCodec())
    assert final.current_version(RUN) == 1
    assert len(final.events_for(RUN)) == 1


# --- runs index -----------------------------------------------------------------


def test_runs_index_tracks_appends_and_matches_replay(store: PostgresEventStore) -> None:
    store.append(_started(), expected_version=0)
    store.append(
        PlanCreated(
            event_id=EventId("e2"), run_id=RUN, occurred_at=NOW, sequence=2, plan="inspect"
        ),
        expected_version=1,
    )

    (record,) = store.list_runs()

    assert record.run_id == RUN
    assert record.objective == "repair"
    assert record.status is RunStatus.READY
    assert record.cost_usd == 0.0
    assert record.stop_reason is None
    assert record == summarize_run(RUN, store.events_for(RUN))


def test_runs_index_records_terminal_stop_and_cost(store: PostgresEventStore) -> None:
    store.append(_started(), expected_version=0)
    store.append(
        PlanCreated(
            event_id=EventId("e2"), run_id=RUN, occurred_at=NOW, sequence=2, plan="inspect"
        ),
        expected_version=1,
    )
    store.append(
        BudgetDebited(
            event_id=EventId("debit"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=3,
            usage=UsageDelta(cost_usd=0.5, input_tokens=10, output_tokens=5),
        ),
        expected_version=2,
    )
    store.append(
        RunStopped(
            event_id=EventId("stop"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=4,
            reason=StopReason.CANCELLED,
            summary="operator stop",
        ),
        expected_version=3,
    )

    (record,) = store.list_runs()

    assert record.status is RunStatus.CANCELLED
    assert record.stop_reason is StopReason.CANCELLED
    assert record.cost_usd == 0.5


def test_runs_index_backfills_when_index_rows_are_lost(store: PostgresEventStore) -> None:
    store.append(_started(), expected_version=0)
    with psycopg.connect(DSN) as connection:
        connection.execute("TRUNCATE runs")

    reopened = PostgresEventStore(DSN, codec=JsonEventCodec())

    (record,) = reopened.list_runs()
    assert record.run_id == RUN
    assert record.status is RunStatus.PLANNING
    assert record.objective == "repair"


def test_concurrent_first_initialization_both_succeed(store: PostgresEventStore) -> None:
    """Racing first-openers of a fresh database must all succeed (advisory lock)."""
    del store
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")

    def open_store(_index: int) -> str:
        try:
            PostgresEventStore(DSN, codec=JsonEventCodec())
        except Exception:
            return "failed"
        return "opened"

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(open_store, range(4)))

    assert outcomes == ["opened"] * 4


def test_backfill_then_append_keeps_the_index_equal_to_replay(
    store: PostgresEventStore,
) -> None:
    """A backfill must never revert the index to a pre-append snapshot.

    The rebuild takes the same per-run advisory lock append() holds, so a
    concurrent appender either lands inside the fold or commits after it and
    overwrites the backfilled row with its own transactional upsert — the
    terminal event can never be lost from the index.
    """
    store.append(_started(), expected_version=0)
    with psycopg.connect(DSN) as connection:
        connection.execute("TRUNCATE runs")

    backfilled = PostgresEventStore(DSN, codec=JsonEventCodec())
    appender = PostgresEventStore(DSN, codec=JsonEventCodec())
    appender.append(
        RunStopped(
            event_id=EventId("stop"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=2,
            reason=StopReason.CANCELLED,
            summary="operator stop",
        ),
        expected_version=1,
    )

    (record,) = backfilled.list_runs()

    assert record.status is RunStatus.CANCELLED
    assert record == summarize_run(RUN, backfilled.events_for(RUN))
