"""Service-owned opt-in execution, kernel leases and observable runner heartbeats.

Runner records describe process liveness, never replace authoritative goal events.
Opening the console does not authorize paid work: an operator must explicitly arm
an intake. Only armed goals recover automatically after a process restart.
"""

import fcntl
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import httpx

from loopforge.adapters.gemini_supervisor import GeminiSupervisor, load_gemini_key
from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.adapters.mlx_transport import lab_transport, mlx_ready
from loopforge.entrypoints.campaign_coding import preflight
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL, PreparedGoalConfig, PreparedGoalService


def runner_record(directory: Path) -> dict[str, object]:
    path = directory / "runner.sqlite"
    if not path.exists():
        return {"status": "offline", "armed": False}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT armed,status,heartbeat,error FROM runner WHERE id=1"
            ).fetchone()
    except (sqlite3.DatabaseError, OSError):
        return {"status": "offline", "armed": False, "error": "Runner metadata unavailable"}
    if row is None:
        return {"status": "offline", "armed": False}
    armed, status, heartbeat, error = row
    if status in {"running", "stopping"} and time.time() - heartbeat > 15:
        status = "offline"
    return {"armed": bool(armed), "status": status, "heartbeat_epoch": heartbeat, "error": error}


def record_runner(directory: Path, *, armed: bool, status: str, error: str = "") -> None:
    with sqlite3.connect(directory / "runner.sqlite", timeout=10) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS runner("
            "id INTEGER PRIMARY KEY CHECK(id=1),armed INTEGER,status TEXT,"
            "heartbeat REAL,error TEXT)"
        )
        db.execute(
            "INSERT OR REPLACE INTO runner VALUES(1,?,?,?,?)",
            (int(armed), status, time.time(), error),
        )


def dependencies(directory: Path) -> None:
    filename = (
        "repository-intake.json"
        if (directory / "repository-intake.json").exists()
        else "intake.json"
    )
    cls = RepositoryGoalConfig if filename.startswith("repository") else PreparedGoalConfig
    config = cls.model_validate_json((directory / filename).read_text())
    preflight(config.task)
    if config.campaign.engine == "mlx":
        if not mlx_ready():
            msg = "The MLX server is unavailable"
            raise ValueError(msg)
        lab_transport().close()  # Pinned model revision and renderer must be present.
    else:
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get("http://127.0.0.1:11434/api/tags")
            response.raise_for_status()
            if config.campaign.model not in {m["name"] for m in response.json()["models"]}:
                msg = "Configured local model is unavailable"
                raise ValueError(msg)
    if config.remote_data != "none":
        load_gemini_key(Path(".loopforge/secrets.env"))


class GoalExecutionService:
    def __init__(
        self,
        root: Path,
        *,
        check: Callable[[Path], None] = dependencies,
        drive: Callable[[Path, threading.Event], str | None] | None = None,
        only: Path | None = None,
    ) -> None:
        self.root, self.check, self.drive = root.resolve(), check, drive or self._drive
        # A dedicated runner drives one goal; other armed goals under the root are not its work.
        self.only = only.resolve() if only is not None else None
        if self.only is not None and self.only.parent != self.root:
            msg = "A dedicated runner's goal must be under its root"
            raise ValueError(msg)
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(
            target=self._loop, daemon=True, name="loopforge-goal-service"
        )
        self.thread.start()

    def arm(self, directory: Path) -> None:
        directory = directory.resolve()
        if directory.parent != self.root or not (directory / "goals.sqlite").is_file():
            msg = "Runner requires an existing server-owned goal"
            raise ValueError(msg)
        if self.only is not None and directory != self.only:
            msg = "A dedicated runner arms only its own goal"
            raise ValueError(msg)
        if SQLiteGoalJournal(directory / "goals.sqlite").load(GOAL).terminal:
            msg = "Terminal goals cannot start a runner"
            raise ValueError(msg)
        self.check(directory)
        record_runner(directory, armed=True, status="queued")
        self.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _loop(self) -> None:  # noqa: PLR0912 - one durable queue pass with explicit outcomes
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".execution-service.lock").open("a+b") as lease:
            while not self.stop.is_set():
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.stop.wait(0.2)
            if self.stop.is_set():
                return
            while not self.stop.is_set():
                for directory in sorted(self.root.iterdir()):
                    if directory.is_symlink() or not directory.is_dir():
                        continue
                    if self.only is not None and directory != self.only:
                        continue
                    record = runner_record(directory)
                    if not record["armed"] or record["status"] == "stopped":
                        continue
                    heartbeat_stop = threading.Event()

                    def heartbeat(
                        path: Path = directory, done: threading.Event = heartbeat_stop
                    ) -> None:
                        while not done.wait(3):
                            record_runner(path, armed=True, status="running")

                    pulse = threading.Thread(target=heartbeat, daemon=True)
                    status = "offline"
                    error = ""
                    try:
                        self.check(directory)
                        record_runner(directory, armed=True, status="running")
                        pulse.start()
                        outcome = self.drive(directory, self.stop)
                        status = (
                            "offline"
                            if self.stop.is_set()
                            else "waiting"
                            if outcome == "waiting"
                            else "stopped"
                        )
                    except (
                        ValueError,
                        OSError,
                        RuntimeError,
                        httpx.HTTPError,
                        sqlite3.DatabaseError,
                    ):
                        error = (
                            "Runner unavailable; inspect dependencies "
                            "or competing service ownership"
                        )
                    except Exception as exc:  # A defect must surface, not hang the runner.
                        error = f"Runner failed: {type(exc).__name__}"
                    finally:
                        heartbeat_stop.set()
                        if pulse.is_alive():
                            pulse.join()  # One short SQLite write; settle it before final status.
                        # One corrupt goal must not kill the queue.
                        with suppress(sqlite3.DatabaseError, OSError):
                            record_runner(directory, armed=True, status=status, error=error)
                    if self.stop.is_set():
                        return
                self.stop.wait(1)

    @staticmethod
    def _drive(directory: Path, stop: threading.Event) -> str | None:
        service = (
            RepositoryGoalService(directory)
            if (directory / "repository-intake.json").exists()
            else PreparedGoalService(directory)
        )
        supervisor = None
        try:
            service.harness.nonblocking_goals = True
            service.harness.operator_stop = stop
            service.worker().recover_interrupted()
            initial = service.journal.load(GOAL)
            if initial.terminal:
                return None
            if initial.paused or initial.late_answer_conflict:
                return "waiting"
            supervisor = GeminiSupervisor(load_gemini_key(Path(".loopforge/secrets.env")))
            coordinator = service.coordinator(supervisor)
            while not stop.is_set():
                before = service.journal.load(GOAL)
                state = coordinator.step(GOAL)
                if state.terminal:
                    return None
                if (
                    state.paused
                    or state.late_answer_conflict
                    or (
                        state.sequence == before.sequence
                        and state.observation.status not in {"coding", "reviewing", "queued"}
                    )
                ):
                    return "waiting"
                stop.wait(1)
        finally:
            if supervisor:
                supervisor.close()
            service.close()
