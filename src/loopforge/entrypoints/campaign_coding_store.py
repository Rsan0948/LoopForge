"""Coding task contracts and append-only task events share the campaign transaction."""

import json
import sqlite3
from dataclasses import asdict

from loopforge.domain.coding import CodingEvent, CodingState, reduce_coding
from loopforge.entrypoints.campaign_models import CodingTaskConfig
from loopforge.entrypoints.campaign_store import CampaignStore


class CodingStore:
    def __init__(self, store: CampaignStore) -> None:
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS coding_tasks(
                    id TEXT PRIMARY KEY, campaign TEXT NOT NULL, stage INTEGER NOT NULL,
                    config TEXT NOT NULL, source TEXT NOT NULL, revision TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coding_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL,
                    event TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS coding_no_update
                BEFORE UPDATE ON coding_events BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS coding_no_delete
                BEFORE DELETE ON coding_events BEGIN SELECT RAISE(ABORT,'append only'); END;
            """)

    @staticmethod
    def config(db: sqlite3.Connection, task: str) -> CodingTaskConfig:
        row = db.execute("SELECT config FROM coding_tasks WHERE id=?", (task,)).fetchone()
        if row is None:
            msg = "unknown coding task"
            raise ValueError(msg)
        return CodingTaskConfig.model_validate_json(row["config"])

    @classmethod
    def state(cls, db: sqlite3.Connection, task: str) -> CodingState:
        config = cls.config(db, task)
        state = CodingState()
        for row in db.execute(
            "SELECT event FROM coding_events WHERE task=? ORDER BY sequence", (task,)
        ):
            state = reduce_coding(
                state,
                CodingEvent(**json.loads(row["event"])),
                max_attempts=config.max_attempts,
                max_review_attempts=config.max_review_attempts,
            )
        return state

    @classmethod
    def append(cls, db: sqlite3.Connection, task: str, event: CodingEvent, detail: str) -> None:
        reduce_coding(
            cls.state(db, task),
            event,
            max_attempts=cls.config(db, task).max_attempts,
            max_review_attempts=cls.config(db, task).max_review_attempts,
        )
        db.execute(
            "INSERT INTO coding_events(task,event,detail) VALUES(?,?,?)",
            (task, json.dumps(asdict(event)), detail),
        )

    @classmethod
    def tasks(cls, db: sqlite3.Connection, campaign: str) -> list[dict[str, object]]:
        return [
            {"id": row["id"], "stage": row["stage"], "state": asdict(cls.state(db, row["id"]))}
            for row in db.execute(
                "SELECT id,stage FROM coding_tasks WHERE campaign=? ORDER BY rowid", (campaign,)
            )
        ]
