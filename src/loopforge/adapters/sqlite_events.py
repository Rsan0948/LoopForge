from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from loopforge.domain.events import BudgetDebited, Event, RunStarted, RunStopped
from loopforge.domain.state import RunRecord, status_after_event
from loopforge.domain.types import RunId, RunStatus, StopReason
from loopforge.ports.event_codec import EventCodecPort
from loopforge.ports.state_store import DuplicateEventError, StreamVersionConflictError

STORE_SCHEMA_VERSION = 2

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
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            objective TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            last_occurred_at TEXT NOT NULL,
            cost_usd REAL NOT NULL,
            stop_reason TEXT
        );
        """,
    ),
)


class UnsupportedStoreSchemaError(RuntimeError):
    """Raised when the database schema is newer than this runtime understands."""


@dataclass(slots=True)
class _RunIndexRow:
    """Mutable runs-index projection row folded event-by-event.

    Every field is unambiguously derivable from single events; ``status``
    follows the domain-owned ``status_after_event`` projection (the reducer
    remains the authority — a conformance suite pins the index equal to
    full replay).
    """

    run_id: str
    objective: str
    status: str
    started_at: str
    last_occurred_at: str
    cost_usd: float
    stop_reason: str | None


def fold_run_index_row(row: _RunIndexRow | None, event: Event) -> _RunIndexRow:
    """Fold one event into a runs-index row (None starts a fresh stream row)."""
    status = status_after_event(event)
    if row is None:
        return _RunIndexRow(
            run_id=str(event.run_id),
            objective=event.objective if isinstance(event, RunStarted) else "",
            status=(status.value if status is not None else RunStatus.PLANNING.value),
            started_at=event.occurred_at.isoformat(),
            last_occurred_at=event.occurred_at.isoformat(),
            cost_usd=event.usage.cost_usd if isinstance(event, BudgetDebited) else 0.0,
            stop_reason=event.reason.value if isinstance(event, RunStopped) else None,
        )
    updated = replace(row, last_occurred_at=event.occurred_at.isoformat())
    if status is not None:
        updated = replace(updated, status=status.value)
    if isinstance(event, BudgetDebited):
        updated = replace(updated, cost_usd=updated.cost_usd + event.usage.cost_usd)
    if isinstance(event, RunStopped):
        updated = replace(updated, stop_reason=event.reason.value)
    return updated


def run_record_from_index_row(row: _RunIndexRow) -> RunRecord:
    """Project a runs-index row into the port-level run record."""
    return RunRecord(
        run_id=RunId(row.run_id),
        objective=row.objective,
        status=RunStatus(row.status),
        started_at=datetime.fromisoformat(row.started_at),
        last_occurred_at=datetime.fromisoformat(row.last_occurred_at),
        cost_usd=row.cost_usd,
        stop_reason=StopReason(row.stop_reason) if row.stop_reason is not None else None,
    )


class SQLiteEventStore:
    """Append-only SQLite event store with optimistic stream concurrency.

    Each append is a compare-and-append transaction. A stale writer fails with
    StreamVersionConflictError instead of silently overwriting or reordering a
    stream. The encoded event payload remains the canonical durable body; the
    additional columns support integrity checks and operational inspection.
    The runs index (schema v2) is a mutable projection maintained in the same
    transaction as each append — the events table stays append-only beneath
    it.
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
            self._rebuild_runs_index_if_needed(connection)

    def _rebuild_runs_index_if_needed(self, connection: sqlite3.Connection) -> None:
        """Backfill the v2 runs index for streams persisted before schema v2."""
        indexed = connection.execute("SELECT COUNT(*) FROM runs").fetchone()
        streams = connection.execute("SELECT COUNT(DISTINCT run_id) FROM events").fetchone()
        indexed_count = int(indexed[0]) if indexed is not None else 0
        stream_count = int(streams[0]) if streams is not None else 0
        if indexed_count >= stream_count:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            run_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT run_id FROM events ORDER BY run_id"
                ).fetchall()
            ]
            for run_id in run_ids:
                rows = connection.execute(
                    "SELECT payload FROM events WHERE run_id = ? ORDER BY sequence ASC",
                    (run_id,),
                ).fetchall()
                index_row: _RunIndexRow | None = None
                for row in rows:
                    index_row = fold_run_index_row(index_row, self._codec.decode(str(row[0])))
                if index_row is not None:
                    self._write_index_row(connection, index_row)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _write_index_row(connection: sqlite3.Connection, row: _RunIndexRow) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO runs(
                run_id, objective, status, started_at, last_occurred_at, cost_usd, stop_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
                index_row = connection.execute(
                    "SELECT run_id, objective, status, started_at, last_occurred_at, "
                    "cost_usd, stop_reason FROM runs WHERE run_id = ?",
                    (str(event.run_id),),
                ).fetchone()
                current_row = (
                    _RunIndexRow(
                        run_id=str(index_row[0]),
                        objective=str(index_row[1]),
                        status=str(index_row[2]),
                        started_at=str(index_row[3]),
                        last_occurred_at=str(index_row[4]),
                        cost_usd=float(index_row[5]),
                        stop_reason=(str(index_row[6]) if index_row[6] is not None else None),
                    )
                    if index_row is not None
                    else None
                )
                self._write_index_row(connection, fold_run_index_row(current_row, event))
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

    def list_runs(self) -> tuple[RunRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, objective, status, started_at, last_occurred_at, "
                "cost_usd, stop_reason FROM runs ORDER BY started_at DESC, run_id ASC"
            ).fetchall()
        return tuple(
            run_record_from_index_row(
                _RunIndexRow(
                    run_id=str(row[0]),
                    objective=str(row[1]),
                    status=str(row[2]),
                    started_at=str(row[3]),
                    last_occurred_at=str(row[4]),
                    cost_usd=float(row[5]),
                    stop_reason=str(row[6]) if row[6] is not None else None,
                )
            )
            for row in rows
        )
