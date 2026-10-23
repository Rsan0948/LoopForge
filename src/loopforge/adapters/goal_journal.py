"""Strict append-only goal journal and atomic shared budget reservations."""

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from loopforge.domain.goal_review import (
    ControlAcknowledged,
    ControlRequested,
    ExperimentRecorded,
    QuestionAnswered,
    QuestionOpened,
    ReviewRecorded,
)
from loopforge.domain.goals import (
    ActionObserved,
    ActionRejected,
    CallFailed,
    CallReserved,
    DecisionRecorded,
    GoalCreated,
    GoalEvent,
    GoalPolicy,
    GoalState,
    GoalStopped,
    ResearchRecorded,
    SupervisorDecision,
    WorkerObservation,
    reduce_goal,
)
from loopforge.domain.investigation import FailureKind, ResearchRecord


class StrictWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CreatedWire(StrictWire):
    kind: Literal["created"]
    policy: GoalPolicy


class ReservedWire(StrictWire):
    kind: Literal["reserved"]
    key: str
    maximum_microusd: int
    trace: str = ""


class DecisionWire(StrictWire):
    kind: Literal["decision"]
    decision: SupervisorDecision
    charged_microusd: int
    trace: str


class FailedWire(StrictWire):
    kind: Literal["failed"]
    charged_microusd: int
    reason: str
    retry_at_epoch: int


class ObservedWire(StrictWire):
    kind: Literal["observed"]
    observation: WorkerObservation


class ResearchWire(StrictWire):
    kind: Literal["research"]
    record: ResearchRecord


class RejectedWire(StrictWire):
    kind: Literal["rejected_action"]
    category: FailureKind
    summary: str


class StoppedWire(StrictWire):
    kind: Literal["stopped"]
    status: str
    summary: str


class QuestionOpenedWire(StrictWire):
    kind: Literal["question_opened"]
    topic: str
    deadline_epoch: int
    question: str = ""
    category: str = ""


class QuestionAnsweredWire(StrictWire):
    kind: Literal["question_answered"]
    topic: str
    answer: str
    answered_epoch: int


class ExperimentWire(StrictWire):
    kind: Literal["experiment"]
    topic: str
    branch: str
    checkpoint: str
    recorded_epoch: int


class ControlWire(StrictWire):
    kind: Literal["control"]
    command: str


class AcknowledgedWire(StrictWire):
    kind: Literal["acknowledged"]
    command: str


class ReviewWire(StrictWire):
    kind: Literal["review"]
    disposition: str
    candidate_sha256: str
    commit: str = ""


WireEvent = (
    CreatedWire
    | ReservedWire
    | DecisionWire
    | FailedWire
    | ObservedWire
    | ResearchWire
    | RejectedWire
    | StoppedWire
    | QuestionOpenedWire
    | QuestionAnsweredWire
    | ExperimentWire
    | ControlWire
    | AcknowledgedWire
    | ReviewWire
)

WIRE: TypeAdapter[WireEvent] = TypeAdapter(
    Annotated[
        CreatedWire
        | ReservedWire
        | DecisionWire
        | FailedWire
        | ObservedWire
        | ResearchWire
        | RejectedWire
        | StoppedWire
        | QuestionOpenedWire
        | QuestionAnsweredWire
        | ExperimentWire
        | ControlWire
        | AcknowledgedWire
        | ReviewWire,
        Field(discriminator="kind"),
    ]
)
KINDS = {
    QuestionOpened: "question_opened",
    QuestionAnswered: "question_answered",
    ExperimentRecorded: "experiment",
    ControlRequested: "control",
    ControlAcknowledged: "acknowledged",
    ReviewRecorded: "review",
    GoalCreated: "created",
    CallReserved: "reserved",
    DecisionRecorded: "decision",
    CallFailed: "failed",
    ActionObserved: "observed",
    GoalStopped: "stopped",
    ActionRejected: "rejected_action",
    ResearchRecorded: "research",
}


def encode_goal_event(event: GoalEvent) -> str:
    return json.dumps({"kind": KINDS[type(event)], **asdict(event)}, separators=(",", ":"))


def decode_goal_event(raw: str) -> GoalEvent:  # noqa: PLR0911, PLR0912 - exhaustive wire variants
    wire = WIRE.validate_json(raw)
    match wire:
        case QuestionOpenedWire():
            return QuestionOpened(wire.topic, wire.deadline_epoch, wire.question, wire.category)
        case QuestionAnsweredWire():
            return QuestionAnswered(wire.topic, wire.answer, wire.answered_epoch)
        case ExperimentWire():
            return ExperimentRecorded(wire.topic, wire.branch, wire.checkpoint, wire.recorded_epoch)
        case ControlWire():
            return ControlRequested(wire.command)
        case AcknowledgedWire():
            return ControlAcknowledged(wire.command)
        case ReviewWire():
            return ReviewRecorded(wire.disposition, wire.candidate_sha256, wire.commit)
        case CreatedWire():
            return GoalCreated(wire.policy)
        case ReservedWire():
            return CallReserved(wire.key, wire.maximum_microusd, wire.trace)
        case DecisionWire():
            return DecisionRecorded(wire.decision, wire.charged_microusd, wire.trace)
        case FailedWire():
            return CallFailed(wire.charged_microusd, wire.reason, wire.retry_at_epoch)
        case ObservedWire():
            return ActionObserved(wire.observation)
        case ResearchWire():
            return ResearchRecorded(wire.record)
        case RejectedWire():
            return ActionRejected(wire.category, wire.summary)
        case StoppedWire():
            return GoalStopped(wire.status, wire.summary)


class SQLiteGoalJournal:
    def __init__(self, path: Path, *, envelope_microusd: int = 5_000_000) -> None:
        if type(envelope_microusd) is not int or not 0 < envelope_microusd <= 5_000_000:
            msg = "Invalid shared evaluation budget"
            raise ValueError(msg)
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS goal_budget(
                    id INTEGER PRIMARY KEY CHECK(id=1), cap INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS goal_events(
                    goal TEXT NOT NULL, sequence INTEGER NOT NULL, event TEXT NOT NULL,
                    PRIMARY KEY(goal,sequence));
                CREATE TRIGGER IF NOT EXISTS goals_no_update BEFORE UPDATE ON goal_events
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS goals_no_delete BEFORE DELETE ON goal_events
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS budget_no_update BEFORE UPDATE ON goal_budget
                    BEGIN SELECT RAISE(ABORT,'immutable budget'); END;
                CREATE TRIGGER IF NOT EXISTS budget_no_delete BEFORE DELETE ON goal_budget
                    BEGIN SELECT RAISE(ABORT,'immutable budget'); END;
            """)
            db.execute("INSERT OR IGNORE INTO goal_budget VALUES(1,?)", (envelope_microusd,))
            if db.execute("SELECT cap FROM goal_budget").fetchone()[0] != envelope_microusd:
                msg = "Shared budget is immutable"
                raise ValueError(msg)

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _load(db: sqlite3.Connection, goal: str) -> GoalState:
        state = GoalState()
        for (raw,) in db.execute(
            "SELECT event FROM goal_events WHERE goal=? ORDER BY sequence", (goal,)
        ):
            state = reduce_goal(state, decode_goal_event(raw))
        return state

    def load_transaction(self, db: sqlite3.Connection, goal: str) -> GoalState:
        """Read the projection within an operator-owned journal transaction."""
        return self._load(db, goal)

    def load(self, goal: str) -> GoalState:
        with self.connect() as db:
            return self._load(db, goal)

    def append(self, goal: str, sequence: int, event: GoalEvent) -> GoalState:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._load(db, goal)
            if state.sequence != sequence:
                msg = "Concurrent goal update; reload before continuing"
                raise ValueError(msg)
            result = reduce_goal(state, event)
            if isinstance(event, CallReserved):
                total = sum(
                    s.spent_microusd + s.reserved_microusd
                    for (name,) in db.execute("SELECT DISTINCT goal FROM goal_events")
                    for s in (self._load(db, name),)
                )
                cap = db.execute("SELECT cap FROM goal_budget").fetchone()[0]
                if total + event.maximum_microusd > cap:
                    msg = "Shared inference budget exhausted"
                    raise ValueError(msg)
            db.execute(
                "INSERT INTO goal_events VALUES(?,?,?)",
                (goal, result.sequence, encode_goal_event(event)),
            )
            return result
