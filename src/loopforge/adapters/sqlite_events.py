from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from loopforge.domain.events import Event
from loopforge.domain.types import RunId
from loopforge.ports.event_codec import EventCodecPort
from loopforge.ports.state_store import DuplicateEventError, StreamVersionConflictError

STORE_SCHEMA_VERSION = 1

_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS event_store_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS events (
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (run_id, sequence)
        );

        CREATE INDEX IF NOT EXISTS idx_events_run_id
            ON events (run_id, sequence);

        CREATE TRIGGER IF NOT EXISTS events_are_append_only_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS events_are_append_only_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END;
        """,
    ),
)


class UnsupportedStoreSchemaError(RuntimeError):
    """Raised when the database schema is newer than this runtime understands."""


class SQLiteEventStore:
    """Append-only SQLite event store with optimistic stream concurrency.

    Each append is a compare-and-append transaction. A stale writer fails with
    StreamVersionConflictError instead of silently overwriting or reordering a
    stream. The encoded event payload remains the canonical durable body; the
    additional columns support integrity checks and operational inspection.
    """

    def __init__(self, path: str | Path, *, codec: EventCodecPort) -> None:
        self._path = str(path)
        self._codec = codec
        self._initialize()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_store_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM event_store_migrations"
            ).fetchone()
            current = int(row[0]) if row is not None else 0
            if current > STORE_SCHEMA_VERSION:
                msg_2 = f"store schema {current} is newer than supported {STORE_SCHEMA_VERSION}"
                raise UnsupportedStoreSchemaError(msg_2)
            for version, sql in _MIGRATIONS:
                if version <= current:
                    continue
                # executescript is used because migrations may contain triggers with
                # internal semicolons. The migration and version marker are wrapped
                # in one explicit script transaction.
                escaped_version = int(version)
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + sql
                    + f"\nINSERT OR IGNORE INTO event_store_migrations(version) "
                    f"VALUES ({escaped_version});\nCOMMIT;"
                )

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM event_store_migrations"
            ).fetchone()
            return int(row[0]) if row is not None else 0

    @staticmethod
    def _assert_append_position(
        connection: sqlite3.Connection, event: Event, *, expected_version: int
    ) -> None:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = ?",
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

    def append(self, event: Event, *, expected_version: int) -> int:
        payload = self._codec.encode(event)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_append_position(connection, event, expected_version=expected_version)
                try:
                    connection.execute(
                        """
                        INSERT INTO events(
                            run_id, sequence, event_id, event_type, occurred_at, payload
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(event.run_id),
                            event.sequence,
                            str(event.event_id),
                            type(event).__name__,
                            event.occurred_at.isoformat(),
                            payload,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    if "events.event_id" in str(exc):
                        msg_3 = f"duplicate event id: {event.event_id}"
                        raise DuplicateEventError(msg_3) from exc
                    raise
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return expected_version + 1

    def events_for(self, run_id: RunId) -> tuple[Event, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM events WHERE run_id = ? ORDER BY sequence ASC",
                (str(run_id),),
            ).fetchall()
        return tuple(self._codec.decode(str(row[0])) for row in rows)

    def current_version(self, run_id: RunId) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        return int(row[0]) if row is not None else 0
