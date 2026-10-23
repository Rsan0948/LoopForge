"""SQLite campaign ledger. The shared slot has no automatic expiry."""

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from loopforge.domain.campaign import CampaignEvent, CampaignState, reduce_campaign
from loopforge.entrypoints.campaign_models import CampaignConfig


class CampaignStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS campaign_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, campaign TEXT NOT NULL,
                    event TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS campaign_events_by_campaign_sequence
                    ON campaign_events(campaign, sequence);
                CREATE TABLE IF NOT EXISTS slot(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    campaign TEXT NOT NULL, task TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dispatch_requests(
                    campaign TEXT NOT NULL, key TEXT NOT NULL, task TEXT NOT NULL,
                    PRIMARY KEY(campaign,key));
                CREATE TRIGGER IF NOT EXISTS dispatch_requests_no_update
                BEFORE UPDATE ON dispatch_requests BEGIN SELECT RAISE(ABORT, 'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS dispatch_requests_no_delete
                BEFORE DELETE ON dispatch_requests BEGIN SELECT RAISE(ABORT, 'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS campaign_events_no_update
                BEFORE UPDATE ON campaign_events BEGIN SELECT RAISE(ABORT, 'append only'); END;
                CREATE TRIGGER IF NOT EXISTS campaign_events_no_delete
                BEFORE DELETE ON campaign_events BEGIN SELECT RAISE(ABORT, 'append only'); END;
            """)

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def config(db: sqlite3.Connection, campaign: str) -> CampaignConfig:
        row = db.execute("SELECT config FROM campaigns WHERE id=?", (campaign,)).fetchone()
        if row is None:
            msg = "unknown campaign"
            raise ValueError(msg)
        config = CampaignConfig.model_validate_json(row["config"])
        change = db.execute(
            "SELECT event FROM campaign_events WHERE campaign=? "
            "AND json_extract(event,'$.kind')='model_changed' ORDER BY sequence DESC LIMIT 1",
            (campaign,),
        ).fetchone()
        if change is not None:
            config = CampaignConfig.model_validate(
                config.model_dump() | {"model": json.loads(change["event"])["model"]}
            )
        return config

    @classmethod
    def state(cls, db: sqlite3.Connection, campaign: str) -> CampaignState:
        config = cls.config(db, campaign)
        state = CampaignState()
        for row in db.execute(
            "SELECT event FROM campaign_events WHERE campaign=? ORDER BY sequence", (campaign,)
        ):
            event = CampaignEvent(**json.loads(row["event"]))
            state = reduce_campaign(state, event, task_count=config.rounds * len(config.scope) * 7)
        return state

    @classmethod
    def append(
        cls,
        db: sqlite3.Connection,
        campaign: str,
        event: CampaignEvent,
        detail: str,
    ) -> CampaignState:
        config = cls.config(db, campaign)
        state = reduce_campaign(
            cls.state(db, campaign),
            event,
            task_count=config.rounds * len(config.scope) * 7,
        )
        db.execute(
            "INSERT INTO campaign_events(campaign,event,detail) VALUES(?,?,?)",
            (campaign, json.dumps(asdict(event)), detail),
        )
        return state

    def snapshot(self, campaign: str, *, include_history: bool = True) -> dict[str, object]:
        with self.connect() as db:
            config = self.config(db, campaign)
            state = self.state(db, campaign)
            history = (
                [
                    dict(row)
                    for row in db.execute(
                        "SELECT sequence,event,detail FROM campaign_events WHERE campaign=? "
                        "ORDER BY sequence",
                        (campaign,),
                    )
                ]
                if include_history
                else []
            )
            slot = db.execute("SELECT campaign,task FROM slot").fetchone()
            return {
                "event_cursor": db.execute(
                    "SELECT coalesce(max(sequence),0) FROM campaign_events WHERE campaign=?",
                    (campaign,),
                ).fetchone()[0],
                "id": campaign,
                "config": config.model_dump(),
                "state": asdict(state),
                "history": history,
                "slot": dict(slot) if slot else None,
            }
