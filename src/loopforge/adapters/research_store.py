"""Content-addressed evidence and immutable operation receipts, not goal authority."""

import hashlib
import json
from dataclasses import asdict

from loopforge.adapters.goal_journal import ResearchWire, SQLiteGoalJournal
from loopforge.domain.investigation import ResearchRecord


class ResearchStore:
    def __init__(self, journal: SQLiteGoalJournal) -> None:
        self.journal = journal
        with journal.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS research_artifacts(
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_receipts(
                    key TEXT PRIMARY KEY, intent TEXT NOT NULL, record TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS artifacts_no_update BEFORE UPDATE ON research_artifacts
                    BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
                CREATE TRIGGER IF NOT EXISTS artifacts_no_delete BEFORE DELETE ON research_artifacts
                    BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
                CREATE TRIGGER IF NOT EXISTS research_no_update BEFORE UPDATE ON research_receipts
                    BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;
                CREATE TRIGGER IF NOT EXISTS research_no_delete BEFORE DELETE ON research_receipts
                    BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;
            """)

    def prior(self, key: str, intent: str) -> ResearchRecord | None:
        with self.journal.connect() as db:
            row = db.execute(
                "SELECT intent,record FROM research_receipts WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        if row[0] != intent:
            msg = "Investigation operation key reused with different intent"
            raise ValueError(msg)
        return ResearchWire.model_validate_json(row[1]).record

    def put(self, key: str, intent: str, record: ResearchRecord, raw: str) -> ResearchRecord:
        if hashlib.sha256(raw.encode()).hexdigest() != record.content_sha256:
            msg = "Evidence content does not match its identity"
            raise ValueError(msg)
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO research_artifacts VALUES(?,?,?)",
                (record.record_id, record.content_sha256, raw),
            )
            row = db.execute(
                "SELECT digest,body FROM research_artifacts WHERE id=?", (record.record_id,)
            ).fetchone()
            if row != (record.content_sha256, raw):
                msg = "Evidence identity collision"
                raise ValueError(msg)
            db.execute(
                "INSERT INTO research_receipts VALUES(?,?,?)",
                (key, intent, json.dumps({"kind": "research", "record": asdict(record)})),
            )
        return record

    def publish_worker(self, raw: str, revision: str, candidate: str) -> ResearchRecord:
        if len(raw.encode()) > 4_000_000:
            msg = "Worker artifact exceeds supported bounds"
            raise ValueError(msg)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        key = "worker:" + hashlib.sha256((revision + candidate + digest).encode()).hexdigest()
        prior = self.prior(key, digest)
        if prior:
            return prior
        record = ResearchRecord(
            record_id="e_" + key[7:31],
            operation_key=key,
            kind="worker",
            revision=revision,
            reference="worker-result",
            content_sha256=digest,
            summary="Worker result; retrieve with byte cursor",
            excerpt="",
            candidate_sha256=candidate,
        )
        return self.put(key, digest, record, raw)

    def owns_worker(self, record_id: str) -> bool:
        with self.journal.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM research_receipts WHERE "
                    "json_extract(record,'$.record.record_id')=? AND "
                    "json_extract(record,'$.record.kind')='worker'",
                    (record_id,),
                ).fetchone()
                is not None
            )

    def original(self, record_id: str) -> str:
        with self.journal.connect() as db:
            row = db.execute(
                "SELECT digest,body FROM research_artifacts WHERE id=?", (record_id,)
            ).fetchone()
        if row is None or hashlib.sha256(row[1].encode()).hexdigest() != row[0]:
            msg = "Original evidence is absent or has changed"
            raise ValueError(msg)
        return row[1]
