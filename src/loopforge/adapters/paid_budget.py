"""Atomic append-only multi-role cash reservations over SQLite."""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from loopforge.domain.paid_budget import PaidAccount, PaidReserved, PaidSettled, reduce_paid


class SQLitePaidBudget:
    def __init__(self, path: Path, ceiling: int) -> None:
        if type(ceiling) is not int or not 0 < ceiling <= 5_000_000:
            msg = "Invalid parent paid cap"
            raise ValueError(msg)
        self.path, self.ceiling = path, ceiling
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paid_policy(
                    id INTEGER PRIMARY KEY CHECK(id=1),cap INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS paid_events(
                    sequence INTEGER PRIMARY KEY,kind TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS paid_no_update
                    BEFORE UPDATE ON paid_events BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS paid_no_delete
                    BEFORE DELETE ON paid_events BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS paid_cap_update
                    BEFORE UPDATE ON paid_policy BEGIN SELECT RAISE(ABORT,'immutable cap'); END;
                CREATE TRIGGER IF NOT EXISTS paid_cap_delete
                    BEFORE DELETE ON paid_policy BEGIN SELECT RAISE(ABORT,'immutable cap'); END;
            """)
            db.execute("INSERT OR IGNORE INTO paid_policy VALUES(1,?)", (ceiling,))
            if db.execute("SELECT cap FROM paid_policy").fetchone()[0] != ceiling:
                msg = "Parent paid cap is immutable"
                raise ValueError(msg)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def load(self, db: sqlite3.Connection) -> PaidAccount:
        state = PaidAccount(self.ceiling)
        for kind, body in db.execute("SELECT kind,body FROM paid_events ORDER BY sequence"):
            values = json.loads(body)
            event = PaidReserved(**values) if kind == "reserved" else PaidSettled(**values)
            state = reduce_paid(state, event)
        return state

    def account(self) -> PaidAccount:
        with self.connect() as db:
            return self.load(db)

    def append(self, event: PaidReserved | PaidSettled) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reduce_paid(self.load(db), event)
            db.execute(
                "INSERT INTO paid_events(kind,body) VALUES(?,?)",
                (
                    "reserved" if isinstance(event, PaidReserved) else "settled",
                    json.dumps(asdict(event)),
                ),
            )

    def bootstrap(self, known: int, reserved: int) -> None:
        """Import pre-parent receipts once, atomically, before permitting role calls."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS paid_bootstrap(id INTEGER PRIMARY KEY CHECK(id=1))"
            )
            if db.execute("SELECT 1 FROM paid_bootstrap").fetchone():
                return
            state = self.load(db)
            if state.keys:
                msg = "Cannot bootstrap a parent ledger after role calls"
                raise ValueError(msg)
            events: list[PaidReserved | PaidSettled] = []
            if known:
                events.extend(
                    [
                        PaidReserved("historical-known", known, "historical"),
                        PaidSettled("historical-known", known),
                    ]
                )
            if reserved:
                events.append(PaidReserved("historical-unknown", reserved, "historical"))
            for event in events:
                state = reduce_paid(state, event)
                db.execute(
                    "INSERT INTO paid_events(kind,body) VALUES(?,?)",
                    (
                        "reserved" if isinstance(event, PaidReserved) else "settled",
                        json.dumps(asdict(event)),
                    ),
                )
            db.execute("INSERT INTO paid_bootstrap VALUES(1)")

    def reserve(self, key: str, role: str, maximum: int) -> None:
        self.append(PaidReserved(key, maximum, role))

    def settle(self, key: str, charged: int) -> None:
        self.append(PaidSettled(key, charged))
