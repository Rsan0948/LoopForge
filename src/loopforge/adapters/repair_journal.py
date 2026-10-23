"""Append-only repair ledger: unknown outcomes retain the full reservation."""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from loopforge.domain.repair_escalation import (
    RepairAccount,
    RepairReserved,
    RepairSettled,
    reduce_repair,
)


class RepairJournal:
    def __init__(self, path: Path, ceiling: int) -> None:
        self.path, self.ceiling = path, ceiling
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS policy(ceiling INTEGER NOT NULL)")
            if not db.execute("SELECT ceiling FROM policy").fetchone():
                db.execute("INSERT INTO policy VALUES(?)", (ceiling,))
            if db.execute("SELECT ceiling FROM policy").fetchone()[0] != ceiling:
                msg = "Repair budget is immutable"
                raise ValueError(msg)
            db.execute(
                "CREATE TABLE IF NOT EXISTS events("
                "sequence INTEGER PRIMARY KEY,kind TEXT NOT NULL,body TEXT NOT NULL,"
                "evidence TEXT NOT NULL)"
            )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def load(self, db: sqlite3.Connection) -> RepairAccount:
        state = RepairAccount(self.ceiling)
        for kind, raw in db.execute("SELECT kind,body FROM events ORDER BY sequence"):
            event = (
                RepairReserved(**json.loads(raw))
                if kind == "reserved"
                else RepairSettled(**json.loads(raw))
            )
            state = reduce_repair(state, event)
        return state

    def append(self, event: RepairReserved | RepairSettled, evidence: str) -> RepairAccount:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = reduce_repair(self.load(db), event)
            db.execute(
                "INSERT INTO events(kind,body,evidence) VALUES(?,?,?)",
                (
                    "reserved" if isinstance(event, RepairReserved) else "settled",
                    json.dumps(asdict(event)),
                    evidence,
                ),
            )
        return state

    def latest(self) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT evidence FROM events WHERE kind='settled' ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None
