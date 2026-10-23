"""Composition bridge to the existing single-worker campaign execution engine."""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.research_store import ResearchStore
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.application.failure_recovery import worker_failure
from loopforge.domain.events import ActionProposed, Event
from loopforge.domain.goals import GoalAction, WorkerObservation
from loopforge.domain.investigation import FailureKind
from loopforge.domain.state import replay
from loopforge.domain.types import RunId, RunStatus
from loopforge.entrypoints.campaign import CampaignHarness
from loopforge.entrypoints.campaign_coding import (
    CodingWorkspace,
    acceptance_contract_hash,
    patch_identity,
    prepare_checkout,
    require_acceptance_detail,
    writable,
)
from loopforge.entrypoints.campaign_models import CodingCommand, CodingTaskConfig, OperatorCommand
from loopforge.entrypoints.goal_checkpoint import capture


class CampaignGoalWorker:
    """A prepared task, never a model-authored command or contract.

    Caller owns the campaign service lock. Operation intent is journaled before
    enqueue/retry/dispatch; task state and counters reconcile an interrupted call.
    This bridge never ticks generic review lanes or commits a candidate.
    """

    def __init__(  # noqa: PLR0913 - explicit boundary dispatch/normalization
        self,
        harness: CampaignHarness,
        campaign: str,
        contract: CodingTaskConfig,
        contract_sha256: str,
        *,
        version: int = 1,
        seed: Path | None = None,
        seed_manifest_sha256: str = "",
        guidance: Callable[[], str] | None = None,
    ) -> None:
        if not contract.acceptance_probes or acceptance_contract_hash(contract) != contract_sha256:
            msg = "Prepared goal requires a frozen trusted acceptance contract"
            raise ValueError(msg)
        self.harness, self.campaign = harness, campaign
        self._contract_json = contract.model_dump_json()
        self.contract_sha256 = contract_sha256
        self.seed = seed
        self.seed_manifest_sha256 = seed_manifest_sha256
        self.guidance = guidance
        identity = campaign if version == 1 else f"{campaign}:milestone:{version}"
        self.task = "goal_" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        with harness.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS goal_worker_operations(
                key TEXT PRIMARY KEY, task TEXT NOT NULL, action TEXT NOT NULL,
                attempts INTEGER NOT NULL, reviews INTEGER NOT NULL,
                stage TEXT NOT NULL DEFAULT 'coding')""")

        with harness.store.connect() as db:
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(goal_worker_operations)")
            }
            if "stage" not in columns:
                db.execute(
                    "ALTER TABLE goal_worker_operations "
                    "ADD COLUMN stage TEXT NOT NULL DEFAULT 'coding'"
                )

    @property
    def contract(self) -> CodingTaskConfig:
        return CodingTaskConfig.model_validate_json(self._contract_json)

    def workspace(self) -> CodingWorkspace:
        """Read the isolated candidate with its original baseline."""
        return self._workspace()

    def _workspace(self) -> CodingWorkspace:
        with self.harness.store.connect() as db:
            row = db.execute(
                "SELECT revision FROM coding_tasks WHERE id=?", (self.task,)
            ).fetchone()
        workspace = GitWorkspaceManager(self.harness.directory / "coding").adopt_existing(
            self.harness.directory / "coding" / self.task
        )
        return CodingWorkspace.wrap(
            replace(workspace, _base_revision=row["revision"], _include_ignored=True)
        )

    def observe(self) -> WorkerObservation:
        with self.harness.store.connect() as db:
            if not db.execute("SELECT 1 FROM coding_tasks WHERE id=?", (self.task,)).fetchone():
                return WorkerObservation()
            actual = self.harness.coding.jobs.config(db, self.task)
            if acceptance_contract_hash(actual) != self.contract_sha256:
                msg = "Prepared worker contract changed"
                raise ValueError(msg)
            state = self.harness.coding.jobs.state(db, self.task)
            row = db.execute(
                "SELECT detail FROM coding_events WHERE task=? AND "
                "json_extract(event,'$.kind') IN ('patched','reviewed','failed') "
                "ORDER BY sequence DESC LIMIT 1",
                (self.task,),
            ).fetchone()
        detail = cast("dict[str, object]", json.loads(row["detail"])) if row else {}
        candidate_dir = self.harness.directory / "coding" / self.task
        changed = sorted(self._workspace().status().files) if row and candidate_dir.exists() else []
        run_events = (
            SQLiteEventStore(
                self.harness.directory / "runs.sqlite", codec=JsonEventCodec()
            ).events_for(RunId(str(detail["run_id"])))
            if row and isinstance(detail.get("run_id"), str)
            else ()
        )
        # Only task evidence crosses the supervisor boundary: no environment,
        # credentials, host workspace paths or raw provider request observations.
        measured = measured_summary(detail, changed, run_events) if row else None
        evidence = json.dumps(
            {
                **({"measured_summary": measured} if measured is not None else {}),
                **{
                    key: detail[key]
                    for key in (
                        "patch",
                        "report",
                        "last_verification",
                        "stop_summary",
                        "acceptance_receipt",
                    )
                    if key in detail
                },
            }
        )
        artifact_ids: tuple[str, ...] = ()
        identity = patch_identity(self._workspace()) if candidate_dir.exists() else ""
        goal_path = self.harness.directory.parent / "goals.sqlite"
        if goal_path.exists() and evidence != "{}":
            artifact = ResearchStore(SQLiteGoalJournal(goal_path)).publish_worker(
                evidence, actual.branch, identity
            )
            artifact_ids = (artifact.record_id,)
        # Explicit omission is preferable to slicing a serialized evidence object.
        if len(evidence.encode()) > 24_000:
            evidence = json.dumps(
                {
                    "omitted": "Retrieve the full worker artifact with retrieve and offset_bytes",
                    **({"measured_summary": measured} if measured is not None else {}),
                    "artifact_ids": artifact_ids,
                    "failure_summary": str(detail.get("stop_summary", ""))[:1000],
                    "patch_sha256": detail.get("patch_sha256", ""),
                }
            )
        verified = False
        if state.status == "patch_ready":
            workspace = self._workspace()
            identity = patch_identity(workspace)
            try:
                receipt = require_acceptance_detail(self.contract, workspace, detail)
                if receipt is not None:
                    events = SQLiteEventStore(
                        self.harness.directory / "runs.sqlite", codec=JsonEventCodec()
                    )
                    verified = (
                        replay(
                            RunId(receipt.run_id), events.events_for(RunId(receipt.run_id))
                        ).status
                        == RunStatus.SUCCEEDED
                    )
            except ValueError:
                evidence = json.dumps(
                    {"error": "Candidate or acceptance evidence changed after review"}
                )
        failure = FailureKind.NONE
        if state.status == "attention":
            recorded_kind = detail.get("failure_kind")
            if isinstance(recorded_kind, str):
                failure = FailureKind(recorded_kind)
            elif isinstance(detail.get("run_id"), str):
                events = SQLiteEventStore(
                    self.harness.directory / "runs.sqlite", codec=JsonEventCodec()
                )
                failure = worker_failure(events.events_for(RunId(str(detail["run_id"]))))
            else:
                failure = FailureKind.UNKNOWN
        return WorkerObservation(state.status, evidence, identity, verified, failure, artifact_ids)

    def recover_interrupted(self) -> None:
        with self.harness.store.connect() as db:
            state = self.harness.store.state(db, self.campaign)
        if state.status == "running" and not (
            self.harness.worker and self.harness.worker.is_alive()
        ):
            self.harness.recover(
                self.campaign,
                OperatorCommand(
                    command="cancel",
                    evidence="Reconcile interrupted worker before any successor",
                ),
            )

    def reconcile(self) -> WorkerObservation:
        with self.harness.store.connect() as db:
            operation = db.execute(
                "SELECT key,action FROM goal_worker_operations WHERE task=? "
                "ORDER BY rowid DESC LIMIT 1",
                (self.task,),
            ).fetchone()
        if operation is None:
            msg = "No durable intent for interrupted worker"
            raise ValueError(msg)
        return self.execute(GoalAction(operation["action"]), operation["key"])

    def execute(self, action: GoalAction, key: str) -> WorkerObservation:  # noqa: PLR0912, PLR0915 - explicit boundary dispatch/normalization
        if self.guidance is not None:
            self.harness.coding.assignment_guidance[self.task] = self.guidance()
        if self.harness.service_lock is None:
            msg = "Goal execution requires exclusive campaign ownership"
            raise ValueError(msg)
        if action not in {GoalAction.DELEGATE, GoalAction.VERIFY, GoalAction.RETRY}:
            msg = "Unsupported prepared worker action"
            raise ValueError(msg)
        self.recover_interrupted()
        with self.harness.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM goal_worker_operations WHERE key=?", (key,)
            ).fetchone()
            exists = db.execute("SELECT 1 FROM coding_tasks WHERE id=?", (self.task,)).fetchone()
            state = self.harness.coding.jobs.state(db, self.task) if exists else None
            if prior:
                if prior["task"] != self.task or prior["action"] != action.value:
                    msg = "Worker operation key reused with different intent"
                    raise ValueError(msg)
                attempts, reviews = prior["attempts"], prior["reviews"]
                stage = prior["stage"]
            else:
                expected = {
                    GoalAction.DELEGATE: "absent",
                    GoalAction.VERIFY: "review_pending",
                    GoalAction.RETRY: "attention",
                }[action]
                if (state.status if state else "absent") != expected:
                    msg = "Prepared worker action denied by current task state"
                    raise ValueError(msg)
                attempts, reviews = (state.attempts, state.review_attempts) if state else (0, 0)
                stage = state.failure_stage if state and state.failure_stage else "coding"
                db.execute(
                    "INSERT INTO goal_worker_operations VALUES(?,?,?,?,?,?)",
                    (key, self.task, action.value, attempts, reviews, stage),
                )
        if action == GoalAction.DELEGATE and not exists:
            self.harness.coding.enqueue(self.campaign, self.task, self.contract)
        if action == GoalAction.DELEGATE and self.seed is not None:
            with self.harness.store.connect() as db:
                current = self.harness.coding.jobs.state(db, self.task)
            if current.attempts == 0:
                self.seed_candidate()
        if (
            action == GoalAction.RETRY
            and state
            and state.status == "attention"
            and state.attempts == attempts
            and state.review_attempts == reviews
        ):
            self.harness.coding.command(
                self.campaign,
                self.task,
                CodingCommand(
                    command="retry",
                    evidence="Bounded successor using recorded failure evidence",
                ),
            )
        with self.harness.store.connect() as db:
            current = self.harness.coding.jobs.state(db, self.task)
        # Counters advance when execution starts. Never dispatch a successor
        # under an old key after crash recovery turned that run into attention.
        should_dispatch = (
            current.attempts == attempts and current.status == "queued"
            if action in {GoalAction.DELEGATE, GoalAction.RETRY}
            else current.review_attempts == reviews and current.status == "review_pending"
        )
        # Retry-review preserves the coding attempt count and increments review count.
        if (
            action == GoalAction.RETRY
            and stage == "reviewing"
            and current.status == "review_pending"
        ):
            should_dispatch = current.review_attempts == reviews
        active = bool(self.harness.worker and self.harness.worker.is_alive())
        if should_dispatch and not active:
            self._dispatch(key)
        with self.harness.store.connect() as db:
            nonblocking = (
                self.harness.nonblocking_goals
                or self.harness.store.config(db, self.campaign).repair_escalation
            )
        if self.harness.worker and not nonblocking:
            self.harness.worker.join()
        observation = self.observe()
        if observation.status == "queued" and not (
            nonblocking and self.harness.worker and self.harness.worker.is_alive()
        ):
            msg = "Prepared worker failed before starting; inspect local campaign events"
            raise ValueError(msg)
        return observation

    def seed_candidate(self) -> None:
        """Reapply an interrupted seed before dispatch; bind bytes/modes to its receipt."""
        assert self.seed is not None
        manifest_path = self.seed.parent / "checkpoint.json"
        raw = manifest_path.read_bytes()
        if (
            not self.seed_manifest_sha256
            or hashlib.sha256(raw).hexdigest() != self.seed_manifest_sha256
        ):
            msg = "Seed manifest changed or was not recorded"
            raise ValueError(msg)
        manifest = json.loads(raw)
        captured = capture(self.seed)
        files = {
            n: {"sha256": hashlib.sha256(b).hexdigest(), "mode": mode}
            for n, (b, mode) in captured[1].items()
        }
        if captured[0] != manifest["snapshot_revision"] or files != manifest["files"]:
            msg = "Seed checkpoint changed since assignment"
            raise ValueError(msg)
        with self.harness.store.connect() as db:
            config = self.harness.store.config(db, self.campaign)
        workspace = prepare_checkout(
            Path(config.repository),
            config.revision,
            self.harness.directory / "coding" / self.task,
            self.contract.branch,
        )
        seed_files = captured[1]
        tracked = workspace.run_git("ls-files", "-z").split("\0")
        for name in sorted(set(tracked) | set(seed_files)):
            if not name or not writable(name, self.contract, workspace):
                continue
            target = workspace.root / name
            if (
                target.is_symlink()
                or not target.resolve().is_relative_to(workspace.root)
                or any(
                    parent.is_symlink()
                    for parent in target.parents
                    if parent != workspace.root.parent
                )
            ):
                msg = "Unsafe seed target"
                raise ValueError(msg)
            if name not in seed_files:
                if target.is_file():
                    target.unlink()
            else:
                body, mode = seed_files[name]
                target.parent.mkdir(parents=True, exist_ok=True)
                pending = target.with_name(target.name + ".loopforge-seed-pending")
                pending.write_bytes(body)
                pending.chmod(mode)
                pending.replace(target)
        # Reconciliation repeats these exact writes until dispatch starts an attempt.
        # No seed is applied after a coding attempt has started.

    def _dispatch(self, key: str) -> None:
        with self.harness.store.connect() as db:
            campaign_state = self.harness.store.state(db, self.campaign)
        if campaign_state.status == "attention":
            self.harness.command(
                self.campaign,
                OperatorCommand(
                    command="resume",
                    evidence="Resume the bounded prepared successor after failure",
                ),
            )
        dispatch = self.harness.tick(self.campaign, dispatch_key=key)
        if dispatch.get("existing"):
            # Claimed but never started: task counters above prove there was
            # no worker action. One keyed recovery dispatch is permitted.
            dispatch = self.harness.tick(self.campaign, dispatch_key=key + ":recovered")
        if not dispatch.get("accepted"):
            msg = "Prepared worker dispatch did not start; reconcile before continuing"
            raise ValueError(msg)


_CHECK_STATUS = re.compile(r"(command:[A-Za-z0-9_]+|patch_constraints): (passed|failed)")


def measured_summary(
    detail: dict[str, object], changed_files: list[str], events: Sequence[Event]
) -> dict[str, object]:
    """Code-derived facts about a worker attempt, so no model must infer them from a diff."""
    verification = str(detail.get("last_verification") or "")
    checks: dict[str, str] = {}
    for name, status in _CHECK_STATUS.findall(verification):
        checks.setdefault(name.removeprefix("command:"), status)
    actions: dict[str, int] = {}
    for event in events:
        if isinstance(event, ActionProposed):
            tool = event.proposal.tool_name
            kind = (
                "edits"
                if tool in {"apply_edits", "edit_file", "write_file", "format_python_imports"}
                else "checks"
                if tool.startswith("check_")
                else "reads"
                if tool in {"read_file", "search_files", "repository_map", "find_symbol"}
                else "other"
            )
            actions[kind] = actions.get(kind, 0) + 1
    return {
        "trust": "runtime_measured; computed from the workspace and journal, not model claims",
        "run_id": detail.get("run_id"),
        "run_status": detail.get("run_status"),
        "files_changed_vs_assignment_baseline": changed_files,
        "checks": checks or "no verification recorded for this attempt",
        "worker_actions": actions,
    }
