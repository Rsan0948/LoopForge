"""Prepare a reviewable linked continuation after a conflicting late answer."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.adapters.paid_budget import SQLitePaidBudget
from loopforge.domain.goal_review import QuestionAnswered, QuestionOpened
from loopforge.entrypoints.campaign_coding import CodingWorkspace, patch_identity, prepare_checkout
from loopforge.entrypoints.goal_budget import paid_root
from loopforge.entrypoints.goal_checkpoint import capture, snapshot_checkpoint
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL, PreparedGoalService


def prepare_successor(directory: Path) -> dict[str, object]:  # noqa: PLR0912, PLR0915 - explicit boundary dispatch/normalization
    journal = SQLiteGoalJournal(directory / "goals.sqlite")
    state = journal.load(GOAL)
    if not state.late_answer_conflict or not (state.paused or state.terminal):
        msg = "A conflicting answer requires reconciled paused or terminal work"
        raise ValueError(msg)
    assert state.policy is not None
    config = RepositoryGoalConfig.model_validate_json(
        (directory / "repository-intake.json").read_text()
    )
    candidates = directory / "worker/coding"
    # Task identity is deterministic and the current version is selected explicitly.
    version = max(1, len(state.assignment_versions))
    with sqlite3.connect(directory / "worker/campaigns.sqlite") as db:
        campaign = db.execute("SELECT id FROM campaigns").fetchone()[0]
    identity = campaign if version == 1 else f"{campaign}:milestone:{version}"
    source = candidates / ("goal_" + hashlib.sha256(identity.encode()).hexdigest()[:20])
    if not source.exists() and not state.observation.candidate_sha256:
        # A clarification can conflict before any worker candidate has been created.
        source = prepare_checkout(
            Path(config.campaign.repository),
            config.campaign.revision,
            directory / "handoff-baseline",
            config.task.branch,
        ).root
    workspace = CodingWorkspace.wrap(GitWorkspaceManager(source.parent).adopt_existing(source))
    workspace = replace(workspace, _base_revision=config.campaign.revision)
    candidate = patch_identity(workspace)
    if state.observation.candidate_sha256 and candidate != state.observation.candidate_sha256:
        msg = "Candidate changed since the last observation"
        raise ValueError(msg)
    child = directory.parent / (directory.name[:60] + "-successor-" + str(state.sequence))
    snapshot, revision = snapshot_checkpoint(source, child / "predecessor", capture(source)[0])
    if patch_identity(workspace) != candidate:
        msg = "Candidate changed during handoff"
        raise ValueError(msg)

    root, root_state = paid_root(directory)
    assert root_state.policy is not None
    parent_path = root / "paid-budget.sqlite"
    if not parent_path.exists():
        parent_service = (
            RepositoryGoalService(root)
            if (root / "repository-intake.json").exists()
            else PreparedGoalService(root)
        )
        try:
            parent_service.paid_budget()
        finally:
            parent_service.close()
    account = SQLitePaidBudget(parent_path, root_state.policy.max_cost_microusd).account()
    remaining = account.ceiling - account.spent - sum(p[1] for p in account.pending)
    if remaining <= 0:
        msg = "No parent paid allowance remains"
        raise ValueError(msg)
    raw = config.model_dump(mode="json")
    raw["campaign"] |= {"repository": str(snapshot), "revision": revision}
    child_intake = child / "repository-intake.json"
    # Repeating an acknowledged handoff retains the original child envelope.
    raw["max_cost_microusd"] = (
        RepositoryGoalConfig.model_validate_json(child_intake.read_text()).max_cost_microusd
        if child_intake.exists()
        else remaining
    )
    raw["clarifications"] = [
        asdict(p) for p in (*state.policy.clarifications, *state.dynamic_questions)
    ]
    (child / "parent-link.json").write_text(
        json.dumps(
            {
                "directory": str(directory.resolve()),
                "candidate_sha256": candidate,
                "parent_sequence": state.sequence,
                "reason": "conflicting-late-answer",
            },
            indent=2,
        )
    )
    service = RepositoryGoalService(child)
    try:
        result = service.create(RepositoryGoalConfig.model_validate_json(json.dumps(raw)))
        for q in state.questions:
            if not q.answer:
                continue
            existing = next((known for known in result.questions if known.topic == q.topic), None)
            if existing is not None and existing.answer:
                if existing.answer != q.answer:
                    msg = "Successor answer differs from the frozen handoff"
                    raise ValueError(msg)
                continue
            if existing is None:
                result = service.journal.append(
                    GOAL, result.sequence, QuestionOpened(q.topic, q.deadline_epoch)
                )
            result = service.journal.append(
                GOAL, result.sequence, QuestionAnswered(q.topic, q.answer, q.answered_epoch)
            )
        return {
            "id": child.name,
            "directory": str(child),
            "phase": result.phase,
            "parent_candidate_sha256": candidate,
            "started": False,
        }
    finally:
        service.close()
