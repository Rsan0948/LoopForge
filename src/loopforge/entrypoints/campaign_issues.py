"""Immutable issue contracts and append-only investigation events."""

import json
import sqlite3
from dataclasses import asdict
from typing import Literal

from pydantic import Field, model_validator

from loopforge.domain.issues import IssueEvent, IssueState, reduce_issue
from loopforge.entrypoints.campaign_evidence import source_allowed
from loopforge.entrypoints.campaign_models import StrictRecord
from loopforge.entrypoints.campaign_store import CampaignStore


class Obligation(StrictRecord):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def safe(self) -> "Obligation":
        if not source_allowed(self.path) or self.end_line < self.start_line:
            msg = "invalid evidence obligation"
            raise ValueError(msg)
        return self


class IssueConfig(StrictRecord):
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    objective: str = Field(min_length=10, max_length=4000)
    evidence: str = Field(min_length=10, max_length=4000)
    obligations: list[Obligation] = Field(min_length=1, max_length=8)
    max_attempts: int = Field(default=3, ge=1, le=5)
    max_tokens: int = Field(default=250_000, ge=1000, le=500_000)
    max_seconds: int = Field(default=900, ge=30, le=1800)
    max_source_reads: int = Field(default=12, ge=2, le=20)


class IssueCommand(StrictRecord):
    command: Literal["retry", "reject", "close"]
    evidence: str = Field(min_length=10, max_length=4000)


class IssueStore:
    def __init__(self, store: CampaignStore) -> None:
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS issues(
                    id TEXT PRIMARY KEY, campaign TEXT NOT NULL, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS issue_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, issue TEXT NOT NULL,
                    event TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS issues_immutable BEFORE UPDATE ON issues
                BEGIN SELECT RAISE(ABORT,'immutable issue contract'); END;
                CREATE TRIGGER IF NOT EXISTS issues_no_delete BEFORE DELETE ON issues
                BEGIN SELECT RAISE(ABORT,'immutable issue contract'); END;
                CREATE TRIGGER IF NOT EXISTS issue_events_no_update BEFORE UPDATE ON issue_events
                BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS issue_events_no_delete BEFORE DELETE ON issue_events
                BEGIN SELECT RAISE(ABORT,'append only'); END;
            """)

    @staticmethod
    def config(db: sqlite3.Connection, issue: str) -> IssueConfig:
        row = db.execute("SELECT config FROM issues WHERE id=?", (issue,)).fetchone()
        if row is None:
            msg = "unknown issue"
            raise ValueError(msg)
        return IssueConfig.model_validate_json(row["config"])

    @classmethod
    def state(cls, db: sqlite3.Connection, issue: str) -> IssueState:
        config = cls.config(db, issue)
        state = IssueState()
        for row in db.execute(
            "SELECT event FROM issue_events WHERE issue=? ORDER BY sequence", (issue,)
        ):
            state = reduce_issue(
                state,
                IssueEvent(**json.loads(row["event"])),
                max_attempts=config.max_attempts,
                max_tokens=config.max_tokens,
                max_seconds=config.max_seconds,
            )
        return state

    @classmethod
    def append(cls, db: sqlite3.Connection, issue: str, event: IssueEvent, detail: str) -> None:
        config = cls.config(db, issue)
        reduce_issue(
            cls.state(db, issue),
            event,
            max_attempts=config.max_attempts,
            max_tokens=config.max_tokens,
            max_seconds=config.max_seconds,
        )
        db.execute(
            "INSERT INTO issue_events(issue,event,detail) VALUES(?,?,?)",
            (issue, json.dumps(asdict(event)), detail),
        )

    @classmethod
    def listing(cls, db: sqlite3.Connection, campaign: str) -> list[dict[str, object]]:
        return [
            {
                "id": row["id"],
                "config": cls.config(db, row["id"]).model_dump(),
                "state": asdict(cls.state(db, row["id"])),
                "evidence": [
                    json.loads(event["detail"])
                    for event in db.execute(
                        "SELECT detail FROM issue_events WHERE issue=? ORDER BY sequence",
                        (row["id"],),
                    )
                ],
            }
            for row in db.execute(
                "SELECT id FROM issues WHERE campaign=? ORDER BY rowid", (campaign,)
            )
        ]
