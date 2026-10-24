"""Serial coding dispatch for prepared goals: one campaign ledger, one execution slot.

The Brook/HeliCops review-campaign product (review lanes, issues, its HTTP API and CLI)
was retired on 2026-10-24 (ADR-0021). What remains is the coding and patch-review
dispatch that the goal path drives; it moves to WorkBuddy with the goal modules.
"""

import fcntl
import hashlib
import json
import sqlite3
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.model_lease import ModelLease
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.application.runtime import Runtime
from loopforge.domain.campaign import CampaignEvent
from loopforge.domain.coding import CodingEvent
from loopforge.domain.state import RunState
from loopforge.domain.types import RunId, RunStatus
from loopforge.entrypoints.campaign_execution import CodingCoordinator
from loopforge.entrypoints.campaign_models import CampaignConfig, DispatchRequest, OperatorCommand
from loopforge.entrypoints.campaign_review import build_review
from loopforge.entrypoints.campaign_store import CampaignStore


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


class CampaignHarness:
    nonblocking_goals: bool = False
    provider_request_directory: Path | None = None

    @property
    def goal_features(self) -> bool:
        return self.coding.goal_features

    def __init__(self, directory: Path, *, goal_features: bool = False) -> None:
        self.directory = directory
        self.store = CampaignStore(directory / "campaigns.sqlite")
        self.coding = CodingCoordinator(self.store, directory, goal_features=goal_features)
        self.coding.driver = self.drive_runtime
        self.coding.live_observer = self.observe_provider
        self.model_lease = ModelLease()
        self.worker: threading.Thread | None = None
        self.guard = threading.Lock()
        self.active_live: OllamaModel | None = None
        self.service_lock: BinaryIO | None = None
        # Set by the owning runner on operator stop (e.g. SIGTERM). The coding
        # thread is non-daemon, so without this a stop waits for the whole run.
        self.operator_stop: threading.Event | None = None

    def observe_provider(self, live: OllamaModel | None) -> None:
        self.active_live = live
        if live is not None and self.provider_request_directory is not None:
            directory = self.provider_request_directory

            def record(payload: dict[str, object]) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                digest = hashlib.sha256(body.encode()).hexdigest()
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / (digest + ".json")
                if path.exists() and path.read_text() != body:
                    msg = "Recorded provider prompt changed"
                    raise ValueError(msg)
                path.write_text(body)

            live.request_recorder = record

    def acquire_service(self) -> None:
        handle = (self.directory / "service.lock").open("a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            msg = "another campaign service owns this ledger"
            raise ValueError(msg) from None
        self.service_lock = handle

    def close(self) -> None:
        if self.worker is not None:
            self.worker.join()  # retain ownership until the single worker has actually exited
        if self.service_lock:
            self.service_lock.close()
            self.service_lock = None

    def create(self, config: CampaignConfig) -> str:
        root = Path(config.repository)
        if not root.is_absolute() or not root.is_dir():
            msg = "repository must be an existing absolute path"
            raise ValueError(msg)
        git(root, "cat-file", "-e", f"{config.revision}^{{commit}}")
        campaign = "campaign_" + uuid4().hex[:12]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id FROM campaigns"):
                if self.store.state(db, row["id"]).status not in {"complete", "parked"}:
                    msg = "finish or park the current repository campaign before creating another"
                    raise ValueError(msg)
            db.execute("INSERT INTO campaigns VALUES(?,?)", (campaign, config.model_dump_json()))
        return campaign

    def tick(
        self, campaign: str, dispatch_key: str | None = None, unit_key: str | None = None
    ) -> dict[str, object]:
        if self.service_lock is None:
            msg = "service ownership is required before starting work"
            raise ValueError(msg)
        with self.guard, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            config = self.store.config(db, campaign)
            state = self.store.state(db, campaign)
            if dispatch_key is not None:
                DispatchRequest(dispatch_key=dispatch_key)
                prior = db.execute(
                    "SELECT task FROM dispatch_requests WHERE campaign=? AND key=?",
                    (campaign, dispatch_key),
                ).fetchone()
                if prior:
                    return {
                        "accepted": False,
                        "existing": True,
                        "task_id": prior["task"],
                        "state": asdict(state),
                    }
            task = self.coding.pending(campaign)
            refusal = self._refusal(db, campaign, state.status, task, unit_key)
            if task is None or refusal is not None:
                return {"accepted": False, "state": asdict(state), **(refusal or {})}
            if db.execute("SELECT 1 FROM slot").fetchone():
                msg = "execution slot is occupied; inspect or recover it before continuing"
                raise ValueError(msg)
            self.model_lease.acquire(task)
            try:
                db.execute("INSERT INTO slot VALUES(1,?,?)", (campaign, task))
                if dispatch_key is not None:
                    db.execute(
                        "INSERT INTO dispatch_requests VALUES(?,?,?)",
                        (campaign, dispatch_key, task),
                    )
                self.store.append(db, campaign, CampaignEvent("coding_claimed", task), "{}")
                db.commit()
            except BaseException:
                self.model_lease.release()
                raise
            self.coding.atomic_finish = self._finish

            def work() -> None:
                try:
                    self.coding.drive(campaign, task, config, self.finish)
                finally:
                    self.model_lease.release()

            self.worker = threading.Thread(target=work, daemon=False)
            try:
                self.worker.start()
            except BaseException:
                self.model_lease.release()
                raise
            return {"accepted": True, "task_id": task}

    def _refusal(
        self,
        db: sqlite3.Connection,
        campaign: str,
        status: str,
        task: str | None,
        unit_key: str | None,
    ) -> dict[str, str] | None:
        """Why a coding dispatch cannot start now; an empty reason means not runnable."""
        if unit_key is not None and unit_key != self.unit_key(db, campaign):
            return {"reason": "stale_unit"}
        if task is None:
            return {"reason": "no_coding_task"}
        if self.coding.blocked(campaign):
            return {"reason": "coding_checkpoint"}
        if status not in {"ready", "checkpoint"} or (self.worker and self.worker.is_alive()):
            return {}
        return None

    def unit_key(self, db: sqlite3.Connection, campaign: str) -> str:
        coding = self.coding.pending(campaign)
        if coding:
            task = self.coding.jobs.state(db, coding)
            return f"{coding}:{task.status}:{task.attempts}:{task.review_attempts}"
        state = self.store.state(db, campaign)
        return f"s{state.stage}:{state.cursor}"

    def drive_runtime(self, runtime: Runtime, run_id: RunId) -> RunState:
        state = runtime.state_for(run_id)
        while not state.status.is_terminal and state.status != RunStatus.WAITING_FOR_APPROVAL:
            with self.store.connect() as db:
                slot = db.execute("SELECT campaign FROM slot").fetchone()
                cancelled = (
                    slot is not None and self.store.state(db, slot["campaign"]).cancel_requested
                )
            if cancelled:
                return runtime.cancel(run_id, summary="campaign cancellation acknowledged")
            if self.operator_stop is not None and self.operator_stop.is_set():
                return runtime.cancel(run_id, summary="operator stop requested")
            state = runtime.step(run_id)
        return state

    def finish(self, campaign: str, task: str, *, passed: bool, detail: str) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._finish(db, campaign, task, passed=passed, detail=detail)

    def _finish(
        self, db: sqlite3.Connection, campaign: str, task: str, *, passed: bool, detail: str
    ) -> None:
        self.store.append(
            db, campaign, CampaignEvent("finished" if passed else "failed", task), detail
        )
        db.execute("DELETE FROM slot WHERE campaign=? AND task=?", (campaign, task))

    def command(self, campaign: str, command: OperatorCommand) -> dict[str, object]:
        with self.guard, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if command.command in {"model_changed", "park"} and (
                (self.worker and self.worker.is_alive())
                or db.execute("SELECT 1 FROM slot").fetchone()
            ):
                msg = "model changes and parking require an idle service and no execution slot"
                raise ValueError(msg)
            if command.command == "resume":
                for other in db.execute("SELECT id FROM campaigns WHERE id != ?", (campaign,)):
                    if self.store.state(db, other["id"]).status not in {"complete", "parked"}:
                        msg = "park the other repository campaign before resuming this one"
                        raise ValueError(msg)
            self.store.append(
                db,
                campaign,
                CampaignEvent(command.command, model=command.model),
                command.model_dump_json(),
            )
        return self.store.snapshot(campaign)

    def recover(self, campaign: str, command: OperatorCommand) -> dict[str, object]:
        if self.service_lock is None:
            msg = "service ownership required"
            raise ValueError(msg)
        with self.guard:
            if self.worker and self.worker.is_alive():
                msg = "worker is still alive; its execution slot cannot be released"
                raise ValueError(msg)
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                state = self.store.state(db, campaign)
                if state.status != "running":
                    msg = "recovery is only for interrupted running tasks"
                    raise ValueError(msg)
                if (
                    state.task_id
                    and db.execute(
                        "SELECT 1 FROM coding_tasks WHERE id=?", (state.task_id,)
                    ).fetchone()
                ):
                    outcome = self.coding.jobs.state(db, state.task_id)
                    if outcome.status in {"patch_ready", "review_pending", "attention"}:
                        recorded = db.execute(
                            "SELECT event,detail FROM coding_events WHERE task=? "
                            "AND json_extract(event,'$.kind') IN ('patched','reviewed','failed') "
                            "ORDER BY sequence DESC LIMIT 1",
                            (state.task_id,),
                        ).fetchone()
                        if recorded:
                            self._finish(
                                db,
                                campaign,
                                state.task_id,
                                passed=json.loads(recorded["event"])["kind"] != "failed",
                                detail=recorded["detail"],
                            )
                            db.commit()
                            return self.store.snapshot(campaign)
                # Cancel linked runtime streams through Runtime's event/reducer path.
                row = db.execute(
                    "SELECT detail FROM campaign_events WHERE campaign=? "
                    "AND json_extract(event,'$.kind')='linked' "
                    "AND json_extract(event,'$.task_id')=? ORDER BY sequence DESC LIMIT 1",
                    (campaign, state.task_id),
                ).fetchone()
                if row:
                    config = self.store.config(db, campaign)
                    events = SQLiteEventStore(
                        self.directory / "runs.sqlite", codec=JsonEventCodec()
                    )
                    runtime, _, live = build_review(config, files={}, previous="", store=events)
                    try:
                        runtime.cancel(
                            RunId(json.loads(row["detail"])["run_id"]), summary=command.evidence
                        )
                    finally:
                        if live:
                            live.close()
                if (
                    state.task_id
                    and db.execute(
                        "SELECT 1 FROM coding_tasks WHERE id=?", (state.task_id,)
                    ).fetchone()
                ):
                    coding_state = self.coding.jobs.state(db, state.task_id)
                    if coding_state.status in {"coding", "reviewing"}:
                        self.coding.jobs.append(
                            db, state.task_id, CodingEvent("failed"), command.model_dump_json()
                        )
                self.store.append(
                    db, campaign, CampaignEvent("failed", state.task_id), command.model_dump_json()
                )
                db.execute("DELETE FROM slot WHERE campaign=?", (campaign,))
        return self.store.snapshot(campaign)

    def snapshot(self, campaign: str, *, history: bool = False) -> dict[str, object]:
        snapshot = self.store.snapshot(campaign, include_history=history)
        with self.store.connect() as db:
            state = self.store.state(db, campaign)
            snapshot["coding_tasks"] = self.coding.jobs.tasks(db, campaign)
        pending = self.coding.pending(campaign)
        snapshot["accepted_baseline"] = self.coding.baseline(campaign)[1]
        snapshot["runnable"] = (
            state.status in {"ready", "checkpoint"}
            and pending is not None
            and not self.coding.blocked(campaign)
        )
        snapshot["worker_alive"] = bool(self.worker and self.worker.is_alive())
        snapshot["active_repository"] = (
            snapshot["slot"]["campaign"] if isinstance(snapshot["slot"], dict) else None
        )
        snapshot["provider_request"] = (
            self.active_live.request_tracker.snapshot() if self.active_live else {"phase": "idle"}
        )
        return snapshot
