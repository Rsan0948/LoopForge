"""Local operator controls and an honest morning report over durable goal events."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.goal_journal import SQLiteGoalJournal, encode_goal_event
from loopforge.adapters.paid_budget import SQLitePaidBudget
from loopforge.domain.goal_review import ControlRequested, QuestionAnswered, ReviewRecorded
from loopforge.domain.goals import reduce_goal
from loopforge.entrypoints.campaign_coding import patch_identity
from loopforge.entrypoints.campaign_models import CodingCommand, StrictRecord
from loopforge.entrypoints.goal_budget import paid_root
from loopforge.entrypoints.goal_runner import runner_record
from loopforge.entrypoints.supervisor import GOAL, PreparedGoalConfig, PreparedGoalService


class AcceptedWorkerReceipt(StrictRecord):
    command: Literal["accept"]
    evidence: str = Field(min_length=1, max_length=4000)
    prepared_patch_sha256: None = None
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


def morning_report(directory: Path) -> dict[str, object]:  # noqa: PLR0915 - explicit boundary dispatch/normalization
    journal = SQLiteGoalJournal(directory / "goals.sqlite")
    state = journal.load(GOAL)
    result: dict[str, object] = asdict(state)
    result["runner"] = runner_record(directory)

    parent, parent_state = paid_root(directory)
    cap = parent_state.policy.max_cost_microusd if parent_state.policy else 0
    if (parent / "paid-budget.sqlite").exists() and cap:
        paid = SQLitePaidBudget(parent / "paid-budget.sqlite", cap).account()
        known = paid.spent
        reserved = sum(p[1] for p in paid.pending)
    else:
        known = state.spent_microusd
        reserved = state.reserved_microusd
    result["paid_spent_microusd"] = known
    result["paid_reserved_microusd"] = reserved
    result["paid_remaining_microusd"] = cap - known - reserved
    result["title"] = "Goal morning report"
    result["remaining_microusd"] = (
        (state.policy.max_cost_microusd - state.spent_microusd - state.reserved_microusd)
        if state.policy
        else 0
    )
    result["remaining_microusd"] = cap - known - reserved
    result["execution_status"] = (
        f"{state.control_requested} requested; in-flight work must reconcile"
        if state.control_requested
        else "paused"
        if state.paused
        else state.phase
    )
    result["diff"] = ""
    result["diff_truncated"] = False
    result["candidate_changed_since_verification"] = False
    result["checkpoint"] = (
        json.loads((directory / "checkpoint.json").read_text())
        if (directory / "checkpoint.json").exists()
        else None
    )
    result["delivery"] = (
        "Candidate retained for local review; acceptance commits only its isolated branch"
    )
    # Read artifacts without taking the runner service lock or constructing a worker.
    worker_dir = directory / "worker"
    checkpoint = state.policy.checkpoint_revision if state.policy else ""
    if not checkpoint and (directory / "intake.json").is_file():
        checkpoint = PreparedGoalConfig.model_validate_json(
            (directory / "intake.json").read_text()
        ).campaign.revision
    candidates = worker_dir / "coding"
    if candidates.exists():
        paths = sorted(p for p in candidates.iterdir() if p.is_dir() and not p.is_symlink())
        if state.assignment_versions:
            with sqlite3.connect(f"file:{worker_dir / 'campaigns.sqlite'}?mode=ro", uri=True) as db:
                campaign = db.execute("SELECT id FROM campaigns").fetchone()[0]
            version = len(state.assignment_versions)
            task_seed = campaign if version == 1 else f"{campaign}:milestone:{version}"

            selected = candidates / ("goal_" + hashlib.sha256(task_seed.encode()).hexdigest()[:20])
            paths = [selected] if selected in paths else []
        if len(paths) == 1 and state.policy and checkpoint:
            try:
                # Git diff includes the committed candidate after acceptance.
                workspace = replace(
                    GitWorkspaceManager(candidates).adopt_existing(paths[0]),
                    _base_revision=checkpoint,
                    _include_ignored=True,
                )
                current = patch_identity(workspace)
                result["current_candidate_sha256"] = current
                result["candidate_changed_since_verification"] = bool(
                    state.observation.candidate_sha256
                    and current != state.observation.candidate_sha256
                )
                raw = workspace.diff().encode()
                result["diff"] = raw[:100000].decode("utf-8", errors="replace")
                result["diff_truncated"] = len(raw) > 100000
                result["candidate_directory"] = str(paths[0])
            except (ValueError, OSError):
                result["diff"] = "Candidate diff unavailable; inspect retained local evidence"
    return result


def review_command(
    directory: Path, command: str, topic: str | None = None, answer: str | None = None
) -> dict[str, object]:
    directory = directory.resolve()
    journal = SQLiteGoalJournal(directory / "goals.sqlite")
    if command == "report":
        return morning_report(directory)
    if command in {"accept", "discard"}:
        return _dispose(directory, command)
    event = (
        QuestionAnswered(topic or "", answer or "", int(datetime.now(UTC).timestamp()))
        if command == "answer"
        else ControlRequested(command)
    )
    # Serialize short operator writes; the runner owns external execution separately.
    with journal.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = journal.load_transaction(db, GOAL)
        result = reduce_goal(state, event)
        db.execute(
            "INSERT INTO goal_events VALUES(?,?,?)",
            (GOAL, result.sequence, encode_goal_event(event)),
        )
    return asdict(result)


def _dispose(directory: Path, command: str) -> dict[str, object]:
    from loopforge.entrypoints.repository_supervisor import RepositoryGoalService  # noqa: PLC0415

    service = (
        RepositoryGoalService(directory)
        if (directory / "repository-intake.json").is_file()
        else PreparedGoalService(directory)
    )
    try:
        worker = None
        if command == "accept":
            worker = (
                service.worker().prepared()
                if isinstance(service, RepositoryGoalService)
                else service.worker()
            )
        # Keep operator answers serialized with acceptance. A crash after Git commit
        # is reconciled by the worker's natural acceptance idempotency on retry.
        with service.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = service.journal.load_transaction(db, GOAL)
            desired = "accepted" if command == "accept" else "discarded"
            if state.disposition == desired:
                return asdict(state)
            event = ReviewRecorded(
                desired, state.observation.candidate_sha256, "0" * 40 if command == "accept" else ""
            )
            reduce_goal(state, event)  # authorize before touching the worker
            commit = ""
            if worker:
                service.harness.coding.command(
                    worker.campaign,
                    worker.task,
                    CodingCommand(
                        command="accept",
                        evidence="Explicit local operator disposition",
                    ),
                )
                # Reconcile the immutable acceptance receipt, not a mutable HEAD
                # that may have moved after commit but before goal acknowledgement.
                with service.harness.store.connect() as worker_db:
                    accepted = worker_db.execute(
                        "SELECT detail FROM coding_events WHERE task=? "
                        "AND json_extract(event,'$.kind')='accept' "
                        "ORDER BY sequence DESC LIMIT 1",
                        (worker.task,),
                    ).fetchone()
                if accepted is None:
                    msg = "Worker acceptance has no durable commit receipt"
                    raise ValueError(msg)
                commit = AcceptedWorkerReceipt.model_validate_json(accepted["detail"]).commit
            elif command == "accept":
                msg = "No candidate to accept"
                raise ValueError(msg)
            event = ReviewRecorded(desired, state.observation.candidate_sha256, commit)
            result = reduce_goal(state, event)
            db.execute(
                "INSERT INTO goal_events VALUES(?,?,?)",
                (GOAL, result.sequence, encode_goal_event(event)),
            )
        return asdict(result)
    finally:
        service.close()
