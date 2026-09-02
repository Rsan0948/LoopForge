"""Append-only Postgres event store with optimistic stream concurrency.

Mirrors :class:`loopforge.adapters.sqlite_events.SQLiteEventStore` semantics
exactly (D3): compare-and-append against ``expected_version``,
``StreamVersionConflictError``/``DuplicateEventError`` parity, append-only
enforcement beneath the adapter API (trigger function), and the schema-v2
runs-index projection maintained in the same transaction as each append.

Differences are transport-level only: a per-run advisory transaction lock
replaces SQLite's ``BEGIN IMMEDIATE`` write serialization, and timestamps
are native ``TIMESTAMPTZ``. The encoded event payload remains the canonical
durable body, identical to the SQLite store byte-for-byte.
"""

from __future__ import annotations

from datetime import datetime
from typing import LiteralString

import psycopg
import psycopg.errors

from loopforge.adapters.sqlite_events import (
    _RunIndexRow,  # pyright: ignore[reportPrivateUsage]  # sibling store adapter shares the index projection
    fold_run_index_row,
    run_record_from_index_row,
)
from loopforge.domain.events import Event
from loopforge.domain.state import RunRecord
from loopforge.domain.types import RunId
from loopforge.ports.event_codec import EventCodecPort
from loopforge.ports.state_store import DuplicateEventError, StreamVersionConflictError

STORE_SCHEMA_VERSION = 1

_MIGRATIONS: tuple[tuple[int, tuple[LiteralString, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS event_store_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_events_run_id
                ON events (run_id, sequence)
            """,
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                last_occurred_at TIMESTAMPTZ NOT NULL,
                cost_usd DOUBLE PRECISION NOT NULL,
                stop_reason TEXT
            )
            """,
            """
            CREATE OR REPLACE FUNCTION events_are_append_only() RETURNS trigger
            LANGUAGE plpgsql AS $func$
            BEGIN
                RAISE EXCEPTION 'events are append-only';
            END;
            $func$
            """,
            """
            DROP TRIGGER IF EXISTS events_append_only_update ON events
            """,
            """
            CREATE TRIGGER events_append_only_update
            BEFORE UPDATE ON events
            FOR EACH ROW EXECUTE FUNCTION events_are_append_only()
            """,
            """
            DROP TRIGGER IF EXISTS events_append_only_delete ON events
            """,
            """
            CREATE TRIGGER events_append_only_delete
            BEFORE DELETE ON events
            FOR EACH ROW EXECUTE FUNCTION events_are_append_only()
            """,
        ),
    ),
)


class UnsupportedStoreSchemaError(RuntimeError):
    """Raised when the database schema is newer than this runtime understands."""


class PostgresEventStore:
    """Append-only Postgres event store; see module docstring for semantics."""

    def __init__(self, dsn: str, *, codec: EventCodecPort) -> None:
        if not dsn.strip():
            msg = "postgres dsn cannot be empty"
            raise ValueError(msg)
        self._dsn = dsn
        self._codec = codec
        self._initialize()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn)

    def _initialize(self) -> None:
        with self._connect() as connection:
            # Serialize concurrent first-initializations across processes:
            # without a lock, two openers of a fresh database both run the
            # migration and the loser crashes on duplicate trigger creation.
            connection.execute("SELECT pg_advisory_xact_lock(hashtext('loopforge-store-init'))")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_store_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM event_store_migrations"
            ).fetchone()
            current = int(row[0]) if row is not None else 0
            if current > STORE_SCHEMA_VERSION:
                msg = f"store schema {current} is newer than supported {STORE_SCHEMA_VERSION}"
                raise UnsupportedStoreSchemaError(msg)
            for version, statements in _MIGRATIONS:
                if version <= current:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO event_store_migrations(version) VALUES (%s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (version,),
                )
            self._rebuild_runs_index_if_needed(connection)

    def _rebuild_runs_index_if_needed(self, connection: psycopg.Connection) -> None:
        """Backfill the runs index if events exist without index rows."""
        indexed = connection.execute("SELECT COUNT(*) FROM runs").fetchone()
        streams = connection.execute("SELECT COUNT(DISTINCT run_id) FROM events").fetchone()
        indexed_count = int(indexed[0]) if indexed is not None else 0
        stream_count = int(streams[0]) if streams is not None else 0
        if indexed_count >= stream_count:
            return
        run_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT run_id FROM events ORDER BY run_id"
            ).fetchall()
        ]
        for run_id in run_ids:
            # Take the same per-run advisory transaction lock append() holds:
            # a concurrent appender then either commits before the fold (and
            # is included in it) or waits and overwrites the backfilled row
            # with its own transactional upsert — the index can never revert
            # to a pre-append snapshot.
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,))
            rows = connection.execute(
                "SELECT payload FROM events WHERE run_id = %s ORDER BY sequence ASC",
                (run_id,),
            ).fetchall()
            index_row: _RunIndexRow | None = None
            for row in rows:
                index_row = fold_run_index_row(index_row, self._codec.decode(str(row[0])))
            if index_row is not None:
                self._write_index_row(connection, index_row)

    @staticmethod
    def _write_index_row(connection: psycopg.Connection, row: _RunIndexRow) -> None:
        connection.execute(
            """
            INSERT INTO runs(
                run_id, objective, status, started_at, last_occurred_at, cost_usd, stop_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                objective = EXCLUDED.objective,
                status = EXCLUDED.status,
                last_occurred_at = EXCLUDED.last_occurred_at,
                cost_usd = EXCLUDED.cost_usd,
                stop_reason = EXCLUDED.stop_reason
            """,
            (
                row.run_id,
                row.objective,
                row.status,
                row.started_at,
                row.last_occurred_at,
                row.cost_usd,
                row.stop_reason,
            ),
        )

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM event_store_migrations"
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def append(self, event: Event, *, expected_version: int) -> int:
        payload = self._codec.encode(event)
        with self._connect() as connection:
            # Serialize concurrent writers on this stream for the duration of
            # the transaction; a stale writer then observes the committed
            # version and fails instead of silently reordering.
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(event.run_id),))
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = %s",
                (str(event.run_id),),
            ).fetchone()
            actual = int(row[0]) if row is not None else 0
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
            try:
                connection.execute(
                    """
                    INSERT INTO events(
                        run_id, sequence, event_id, event_type, occurred_at, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(event.run_id),
                        event.sequence,
                        str(event.event_id),
                        type(event).__name__,
                        event.occurred_at,
                        payload,
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                if exc.diag.constraint_name == "events_event_id_key":
                    msg_2 = f"duplicate event id: {event.event_id}"
                    raise DuplicateEventError(msg_2) from exc
                raise
            index_row = connection.execute(
                "SELECT run_id, objective, status, started_at, last_occurred_at, "
                "cost_usd, stop_reason FROM runs WHERE run_id = %s",
                (str(event.run_id),),
            ).fetchone()
            current_row = _index_row_from_db(index_row) if index_row is not None else None
            self._write_index_row(connection, fold_run_index_row(current_row, event))
        return expected_version + 1

    def events_for(self, run_id: RunId) -> tuple[Event, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM events WHERE run_id = %s ORDER BY sequence ASC",
                (str(run_id),),
            ).fetchall()
        return tuple(self._codec.decode(str(row[0])) for row in rows)

    def current_version(self, run_id: RunId) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = %s",
                (str(run_id),),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_runs(self) -> tuple[RunRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, objective, status, started_at, last_occurred_at, "
                "cost_usd, stop_reason FROM runs ORDER BY started_at DESC, run_id ASC"
            ).fetchall()
        return tuple(run_record_from_index_row(_index_row_from_db(row)) for row in rows)


def _index_row_from_db(row: tuple[object, ...]) -> _RunIndexRow:
    return _RunIndexRow(
        run_id=str(row[0]),
        objective=str(row[1]),
        status=str(row[2]),
        started_at=_iso(row[3]),
        last_occurred_at=_iso(row[4]),
        cost_usd=float(row[5]),  # pyright: ignore[reportArgumentType]  # numeric column
        stop_reason=str(row[6]) if row[6] is not None else None,
    )


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
