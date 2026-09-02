"""Server-owned session management: run-driver threads and bundle lifecycle.

This module is the synchronous heart of the operator server (PACS-014 M2).
It owns exactly ONE driver thread per active run (D5): runtime code stays
fully synchronous, and ``Runtime.step()`` is only ever called on the per-run
driver thread while holding the per-run lock — never on an event loop.
Control commands (grant/reject/instruct/cancel) take the same per-run lock,
so a durable command event can never interleave with a mid-step append.

Authority model: the manager is a projection plus command issuer, NEVER a
state writer. Every run-state change flows through the Runtime as durable
events; the manager never appends events directly and never mutates
``RunState``. The :class:`SessionRegistry` is server-owned WIRING metadata
(operator authority — how to reconstruct a bundle), not run state; the
authoritative run state lives exclusively in the event store.

Rollback is a workspace-port operation, not a run-state write: terminal
streams are sealed, so no event can be appended to capture a revert, and
for a live run the next verification artifact captures the resulting diff.
The workspace port (operator-owned wiring) is the honest authority boundary
for reverting the adopted checkout.

Driver lifecycle: a driver thread loops ``step()`` until the run is terminal
or ``WAITING_FOR_APPROVAL``, until a pause is requested, or until an
exception escapes — exceptions are recorded on the session (never crash
silently) and end driving. ``pause()`` sets the pause flag and joins the
driver with a bounded timeout; a step already in flight (for example a
retry-backoff sleep) finishes first, and the pause returns whether or not
the step completed in time.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from loopforge.adapters.fanout_store import FanOutEventStore
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.runtime import UnknownRunError
from loopforge.domain.events import Event
from loopforge.domain.state import RunState, replay
from loopforge.domain.types import ActionId, RunId, RunStatus
from loopforge.entrypoints.cli import build_deepseek_model, build_ollama_model
from loopforge.entrypoints.followup import consolidate_follow_up_report
from loopforge.entrypoints.profile import LoopProfile, ProfileError, load_profile
from loopforge.entrypoints.repair import (
    RepairRuntimeBundle,
    RepairRuntimeDeps,
    build_adopted_repair_runtime,
)
from loopforge.ports.model import ModelPort
from loopforge.ports.state_store import StateStorePort

DEFAULT_JOIN_TIMEOUT_SECONDS = 30.0


class SessionStateError(RuntimeError):
    """Raised when a driving command is illegal for the session's live state."""


class UnmanagedRunError(RuntimeError):
    """Raised when a driving command targets a run the server does not manage."""


class SessionRegistryError(RuntimeError):
    """Raised when the on-disk session registry cannot be decoded.

    The registry fails closed on read: a corrupt file is never silently
    ignored (that would misreport managed sessions as unmanaged), and the
    operator must repair or remove the file.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionWiring:
    """Everything the server needs to reconstruct a run's bundle after restart.

    ``profile_source`` is either ``{"kind": "path", "path": ...}`` (an
    operator-owned profile TOML on disk) or ``{"kind": "inline", "toml": ...}``
    (the validated TOML text rendered from inline REST fields, persisted so a
    restart reconstructs the identical profile).
    """

    objective: str
    repository: str
    profile_source: dict[str, str]
    max_cost_usd: float
    max_iterations: int
    max_total_tokens: int | None
    max_elapsed_seconds: float | None
    model_provider: str
    model_name: str
    container_image: str | None
    created_at: str


class SessionRegistry:
    """JSON-file-backed mapping of run_id to :class:`SessionWiring`.

    This is server-owned wiring metadata (operator authority), NOT run state:
    it records how to rebuild a session's bundle, never anything about the
    run itself. Writes are atomic (tmp file + rename) so a crash mid-write
    cannot corrupt the registry.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        # Sweep orphaned tmp files from crashed writes (write-tmp-then-rename
        # leaves one behind when the process dies between the two steps).
        for stale in self._path.parent.glob(f"{self._path.name}.*.tmp"):
            stale.unlink(missing_ok=True)

    def _read_all(self) -> dict[str, SessionWiring]:
        if not self._path.is_file():
            return {}
        try:
            raw: object = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = f"session registry is not valid JSON: {self._path} ({exc})"
            raise SessionRegistryError(msg) from exc
        if not isinstance(raw, dict):
            msg_2 = f"session registry is not a JSON object: {self._path}"
            raise SessionRegistryError(msg_2)
        entries = cast("dict[object, object]", raw)
        try:
            return {str(run_id): _wiring_from_dict(data) for run_id, data in entries.items()}
        except TypeError as exc:
            msg_3 = f"session registry entry drifted from the wiring schema: {exc}"
            raise SessionRegistryError(msg_3) from exc

    def get(self, run_id: str) -> SessionWiring | None:
        with self._lock:
            return self._read_all().get(run_id)

    def put(self, run_id: str, wiring: SessionWiring) -> None:
        with self._lock:
            data = self._read_all()
            data[run_id] = wiring
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
            payload = {key: asdict(value) for key, value in sorted(data.items())}
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(self._path)

    def as_dict(self) -> dict[str, SessionWiring]:
        with self._lock:
            return self._read_all()


def _require_str_field(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        msg = f"session wiring field {key!r} must be a string"
        raise TypeError(msg)
    return value


def _optional_str_field(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        msg = f"session wiring field {key!r} must be a string or null"
        raise TypeError(msg)
    return value


def _optional_number_field(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"session wiring field {key!r} must be numeric or null"
        raise TypeError(msg)
    return float(value)


def _wiring_from_dict(data: object) -> SessionWiring:
    """Reconstruct wiring from its persisted JSON form (fail closed on drift)."""
    if not isinstance(data, dict):
        msg = "session wiring entry must be a JSON object"
        raise TypeError(msg)
    record = cast("dict[str, Any]", data)
    source = record.get("profile_source")
    if not isinstance(source, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in cast("dict[Any, Any]", source).items()
    ):
        msg_2 = "session wiring field 'profile_source' must be a string map"
        raise TypeError(msg_2)
    max_iterations = record.get("max_iterations")
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        msg_3 = "session wiring field 'max_iterations' must be an integer"
        raise TypeError(msg_3)
    max_total_tokens = record.get("max_total_tokens")
    if max_total_tokens is not None and (
        isinstance(max_total_tokens, bool) or not isinstance(max_total_tokens, int)
    ):
        msg_4 = "session wiring field 'max_total_tokens' must be an integer or null"
        raise TypeError(msg_4)
    max_cost = _optional_number_field(record, "max_cost_usd")
    if max_cost is None:
        msg_5 = "session wiring field 'max_cost_usd' is required"
        raise TypeError(msg_5)
    return SessionWiring(
        objective=_require_str_field(record, "objective"),
        repository=_require_str_field(record, "repository"),
        profile_source={str(k): str(v) for k, v in cast("dict[Any, Any]", source).items()},
        max_cost_usd=max_cost,
        max_iterations=max_iterations,
        max_total_tokens=max_total_tokens,
        max_elapsed_seconds=_optional_number_field(record, "max_elapsed_seconds"),
        model_provider=_require_str_field(record, "model_provider"),
        model_name=_require_str_field(record, "model_name"),
        container_image=_optional_str_field(record, "container_image"),
        created_at=_require_str_field(record, "created_at"),
    )


class BundleFactory(Protocol):
    """Rebuilds a repair runtime bundle from persisted wiring.

    Production reloads the profile from ``profile_source`` and calls
    ``build_adopted_repair_runtime``; tests inject fakes.
    """

    def build(self, wiring: SessionWiring, store: StateStorePort) -> RepairRuntimeBundle: ...


@dataclass(slots=True)
class _Session:
    """Live in-memory session: bundle, per-run lock, and driver bookkeeping."""

    bundle: RepairRuntimeBundle
    lock: threading.Lock = field(default_factory=threading.Lock)
    driver: threading.Thread | None = None
    driving: bool = False
    pause_requested: bool = False
    error: BaseException | None = None


class SessionManager:
    """Owns per-run driver threads, bundles, and session wiring (D5/D9).

    The runtime stays synchronous: ``step()`` runs only on the per-run
    driver thread under the per-run lock, and every control command that
    appends a durable event takes the same lock so command events can never
    interleave with a mid-step append.
    """

    def __init__(
        self,
        store: FanOutEventStore,
        registry: SessionRegistry,
        bundle_factory: BundleFactory,
        *,
        join_timeout_seconds: float = DEFAULT_JOIN_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._registry = registry
        self._factory = bundle_factory
        self._join_timeout = join_timeout_seconds
        self._lock = threading.Lock()
        self._sessions: dict[RunId, _Session] = {}
        # Creates in flight by resolved repository path (see create_session).
        self._creating: set[str] = set()

    # -- session creation ---------------------------------------------------

    def create_session(self, wiring: SessionWiring) -> str:
        """Build a bundle, durably start the run, and register the session.

        ``runtime.start`` leaves the run durable and quiescent (READY) — no
        driving begins until ``start_driving``/``resume`` is called. One
        active (non-terminal) session may adopt a given checkout at a time;
        a second create for the same repository is denied while the first
        session's run is still live. The claim is derived from the durable
        registry and runs index (not process memory), so it survives server
        restarts.
        """
        repository_key = str(Path(wiring.repository).expanduser().resolve())
        with self._lock:
            claimant = self._active_repository_claim(repository_key)
            if claimant is not None:
                msg = (
                    f"repository {repository_key} already has an active session "
                    f"({claimant}); stop it before starting another"
                )
                raise SessionStateError(msg)
            if repository_key in self._creating:
                msg_2 = f"repository {repository_key} already has a session being created"
                raise SessionStateError(msg_2)
            self._creating.add(repository_key)
        try:
            bundle = self._factory.build(wiring, self._store)
            try:
                run_id = bundle.runtime.start(wiring.objective)
            except BaseException:
                # A failed start must not leak the freshly built bundle
                # (model HTTP client, container sandbox).
                bundle.close()
                raise
            with self._lock:
                self._sessions[run_id] = _Session(bundle=bundle)
            try:
                self._registry.put(str(run_id), wiring)
            except BaseException:
                # Registration failed: tear the session down so the durable
                # run is never left live-but-unmanaged after a restart.
                with self._lock:
                    self._sessions.pop(run_id, None)
                # Best-effort cleanup must not mask the registration failure.
                with suppress(Exception):
                    bundle.runtime.cancel(run_id, summary="session registration failed")
                bundle.close()
                raise
            return str(run_id)
        finally:
            with self._lock:
                self._creating.discard(repository_key)

    def _active_repository_claim(self, repository_key: str) -> RunId | None:
        """The live (non-terminal) managed run adopting this checkout, if any."""
        wiring_by_run = self._registry.as_dict()
        for record in self._store.list_runs():
            if record.status.is_terminal:
                continue
            wiring = wiring_by_run.get(str(record.run_id))
            if wiring is None:
                continue
            if str(Path(wiring.repository).expanduser().resolve()) == repository_key:
                return record.run_id
        return None

    # -- driving ------------------------------------------------------------

    def start_driving(self, run_id: RunId | str) -> None:
        """Spawn the per-run driver thread for a managed, non-terminal run."""
        rid = RunId(str(run_id))
        session = self._ensure_session(rid)
        state = session.bundle.runtime.state_for(rid)
        if state.status.is_terminal:
            msg = f"run {rid} is terminal ({state.status.value}); nothing to drive"
            raise SessionStateError(msg)
        with self._lock:
            if session.driving:
                msg_2 = f"run {rid} is already driving"
                raise SessionStateError(msg_2)
            session.pause_requested = False
            session.error = None
            session.driving = True
            session.driver = threading.Thread(
                target=self._drive_loop,
                args=(rid, session),
                name=f"loopforge-driver-{rid}",
                daemon=True,
            )
            session.driver.start()

    def _drive_loop(self, run_id: RunId, session: _Session) -> None:
        """Step the run until terminal, waiting for approval, or paused.

        An escaping exception is recorded on the session and ends driving —
        driver threads must never crash silently.
        """
        try:
            while not session.pause_requested:
                with session.lock:
                    state = session.bundle.runtime.step(run_id)
                if state.status.is_terminal or state.status is RunStatus.WAITING_FOR_APPROVAL:
                    return
        except BaseException as exc:  # recorded on the session, never silent
            session.error = exc
        finally:
            session.driving = False

    def pause(self, run_id: RunId | str) -> None:
        """Request the driver to pause and join it with a bounded timeout.

        A step already in flight (for example a retry-backoff sleep) finishes
        first; the join is bounded so pause never blocks indefinitely. Pausing
        a quiescent, terminal, or bundle-less known run is an idempotent
        no-op; only runs unknown to the store are errors.
        """
        rid = RunId(str(run_id))
        session = self._session_for(rid)
        if session is None:
            if self._store.current_version(rid) == 0:
                msg = f"no persisted run: {rid}"
                raise UnknownRunError(msg)
            return
        session.pause_requested = True
        driver = session.driver
        if driver is not None and driver.is_alive():
            driver.join(timeout=self._join_timeout)

    def resume(self, run_id: RunId | str) -> RunState:
        """Rebuild the bundle if needed (D9), then drive a non-terminal run.

        Runs unknown to the store raise ``UnknownRunError``; runs present in
        the store but absent from the server registry raise
        ``UnmanagedRunError``. Resuming a terminal run is a no-op.
        """
        rid = RunId(str(run_id))
        session = self._ensure_session(rid)
        state = session.bundle.runtime.state_for(rid)
        if state.status.is_terminal:
            return state
        self.start_driving(rid)
        return session.bundle.runtime.state_for(rid)

    # -- operator commands ----------------------------------------------------

    def approve(self, run_id: RunId | str, action_id: str) -> RunState:
        """Durably grant a pending approval, then wake the run."""
        rid = RunId(str(run_id))
        session = self._ensure_session(rid)
        with session.lock:
            session.bundle.runtime.grant_approval(rid, ActionId(action_id))
        return self.resume(rid)

    def reject(self, run_id: RunId | str, action_id: str, reason: str) -> RunState:
        """Durably reject a pending approval, then wake the run to re-plan."""
        rid = RunId(str(run_id))
        session = self._ensure_session(rid)
        with session.lock:
            session.bundle.runtime.reject_approval(rid, ActionId(action_id), reason=reason)
        return self.resume(rid)

    def stop(self, run_id: RunId | str, *, summary: str = "cancelled by operator") -> RunState:
        """Pause the driver, then durably cancel the run through the runtime."""
        rid = RunId(str(run_id))
        self.pause(rid)
        session = self._ensure_session(rid)
        with session.lock:
            return session.bundle.runtime.cancel(rid, summary=summary)

    def instruct(
        self, run_id: RunId | str, instruction: str, *, amend_objective: bool = False
    ) -> RunState:
        """Record an operator instruction after quiescing the driver.

        The reducer only accepts ``OperatorInstruction`` in READY,
        REFLECTING, or WAITING_FOR_APPROVAL, so the driver must be paused
        first — the durable event can then land in a legal state instead of
        racing a mid-step append.
        """
        rid = RunId(str(run_id))
        self.pause(rid)
        session = self._ensure_session(rid)
        with session.lock:
            return session.bundle.runtime.add_operator_instruction(
                rid, instruction, amend_objective=amend_objective
            )

    def rollback(self, run_id: RunId | str, paths: tuple[str, ...] | None = None) -> None:
        """Revert the adopted checkout through the workspace port.

        This is a workspace-port operation, not a run-state write: terminal
        streams are sealed (no event could be appended even in principle),
        and for a live run the next verification artifact captures the
        resulting diff. ``paths is None`` performs a hard ``reset()`` to the
        base revision; otherwise ``checkout(paths)`` reverts selectively.
        Denied while the run is driving — a revert must never race tool
        execution in the same checkout.
        """
        rid = RunId(str(run_id))
        session = self._ensure_session(rid)
        if session.driving:
            msg = f"run {rid} is driving; pause it before rollback"
            raise SessionStateError(msg)
        with session.lock:
            if paths is None:
                session.bundle.workspace.reset()
            else:
                session.bundle.workspace.checkout(paths)

    def follow_up(self, run_id: RunId | str) -> str:
        """Create a quiescent successor session seeded with a terminal run's report.

        The follow-up clones the source run's wiring (same repository,
        profile, model, and budgets) with the objective extended by a
        bounded, deterministic report consolidated from the durable event
        stream (PACS-014b). The new session is left READY but undriven:
        the operator reviews the seeded objective and presses start —
        nothing auto-chains. Denied for non-terminal runs (the source must
        be finished) and unmanaged runs (there is no wiring to clone). The
        repository-exclusivity guard passes because the source run is
        terminal.
        """
        rid = RunId(str(run_id))
        state = self.state(rid)
        if not state.status.is_terminal:
            msg = f"run {rid} is {state.status.value}; follow-up requires a terminal (finished) run"
            raise SessionStateError(msg)
        wiring = self.wiring(rid)
        if wiring is None:
            msg_2 = f"run {rid} is unmanaged; there is no wiring to follow up from"
            raise UnmanagedRunError(msg_2)
        report = consolidate_follow_up_report(str(rid), self.events(rid), state)
        successor = replace(
            wiring,
            objective=f"{state.objective}\n\n{report}",
            created_at=datetime.now(UTC).isoformat(),
        )
        return self.create_session(successor)

    # -- projections ----------------------------------------------------------

    def state(self, run_id: RunId | str) -> RunState:
        """Replay the authoritative stream (identical to runtime.state_for).

        Reads go straight to the store so detail projections work even when
        no live bundle exists in memory (for example after a server restart).
        """
        rid = RunId(str(run_id))
        events = self._store.events_for(rid)
        if not events:
            msg = f"no persisted run: {rid}"
            raise UnknownRunError(msg)
        return replay(rid, events)

    def events(self, run_id: RunId | str) -> tuple[Event, ...]:
        """The run's durable events; unknown runs raise ``UnknownRunError``."""
        rid = RunId(str(run_id))
        events = self._store.events_for(rid)
        if not events:
            msg = f"no persisted run: {rid}"
            raise UnknownRunError(msg)
        return events

    def wiring(self, run_id: RunId | str) -> SessionWiring | None:
        """The registry wiring for a run, or ``None`` when unmanaged."""
        return self._registry.get(str(run_id))

    def session_error(self, run_id: RunId | str) -> BaseException | None:
        """The exception that ended the driver, if one was recorded."""
        session = self._session_for(RunId(str(run_id)))
        return session.error if session is not None else None

    def list_sessions(self) -> list[dict[str, object]]:
        """Merge the store's runs index with registry and live driving flags."""
        managed = self._registry.as_dict()
        sessions: list[dict[str, object]] = []
        for record in self._store.list_runs():
            session = self._session_for(record.run_id)
            sessions.append(
                {
                    "run_id": str(record.run_id),
                    "objective": record.objective,
                    "status": record.status.value,
                    "started_at": record.started_at.isoformat(),
                    "last_occurred_at": record.last_occurred_at.isoformat(),
                    "cost_usd": record.cost_usd,
                    "stop_reason": (
                        record.stop_reason.value if record.stop_reason is not None else None
                    ),
                    "driving": bool(session is not None and session.driving),
                    "managed": str(record.run_id) in managed,
                }
            )
        return sessions

    # -- lifecycle --------------------------------------------------------------

    def shutdown(self) -> None:
        """Pause every driver and close every bundle (app lifespan hook)."""
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.pause_requested = True
        for session in sessions:
            driver = session.driver
            if driver is not None and driver.is_alive():
                driver.join(timeout=self._join_timeout)
            # Close under the per-run lock: an in-flight step holds it, so the
            # bundle (and its container sandbox) is never torn down underneath
            # a running step — close serializes behind the step instead.
            with session.lock:
                session.bundle.close()
        with self._lock:
            self._sessions.clear()

    # -- internals ----------------------------------------------------------------

    def _session_for(self, run_id: RunId) -> _Session | None:
        with self._lock:
            return self._sessions.get(run_id)

    def _ensure_session(self, run_id: RunId) -> _Session:
        """Return the live session, rebuilding the bundle from wiring if needed."""
        session = self._session_for(run_id)
        if session is not None:
            return session
        if self._store.current_version(run_id) == 0:
            msg = f"no persisted run: {run_id}"
            raise UnknownRunError(msg)
        wiring = self._registry.get(str(run_id))
        if wiring is None:
            msg_2 = (
                f"run {run_id} is not managed by this server (no session wiring in the registry)"
            )
            raise UnmanagedRunError(msg_2)
        bundle = self._factory.build(wiring, self._store)
        session = _Session(bundle=bundle)
        with self._lock:
            existing = self._sessions.get(run_id)
            if existing is None:
                self._sessions[run_id] = session
        if existing is not None:
            # Loser of a double-build race: closed AFTER releasing the
            # manager-wide lock so container teardown never stalls unrelated
            # sessions.
            bundle.close()
            return existing
        return session


# -- Inline profile rendering -------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class InlineCheckFields:
    name: str
    kind: str
    argv: tuple[str, ...]
    timeout_seconds: float
    cpu_seconds: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InlineAcceptanceFields:
    required: tuple[str, ...]
    allowed_prefixes: tuple[str, ...]
    require_change: bool = False
    max_changed_files: int | None = None


def _empty_environment() -> Mapping[str, str]:
    return {}


@dataclass(frozen=True, slots=True, kw_only=True)
class InlineSandboxFields:
    container_image: str | None = None
    environment: Mapping[str, str] = field(default_factory=_empty_environment)
    max_memory_bytes: int | None = None
    local_python: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InlineModelFields:
    provider: str
    tier: str
    name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InlineBudgetFields:
    max_cost_usd: float
    max_iterations: int
    max_total_tokens: int | None = None
    max_elapsed_seconds: float | None = None
    no_progress_limit: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InlineProfileFields:
    """Plain-data mirror of the inline profile REST schema (no pydantic here)."""

    repository: str
    objective: str
    checks: tuple[InlineCheckFields, ...]
    acceptance: InlineAcceptanceFields
    model: InlineModelFields
    budget: InlineBudgetFields
    sandbox: InlineSandboxFields = field(default_factory=InlineSandboxFields)
    approval_required_for: tuple[str, ...] = ()
    task_id: str = "server-session"


def _toml_str(value: str) -> str:
    # JSON string escaping is a subset of TOML basic-string escaping.
    return json.dumps(value)


def _toml_str_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_str(value) for value in values) + "]"


def render_inline_profile_toml(fields: InlineProfileFields) -> str:
    """Render inline profile fields into operator-profile TOML text.

    Mirrors the ``.loopforge/*.toml`` schema exactly (task / checks /
    acceptance / sandbox / model / budget); the rendered text is validated by
    ``load_profile`` before any session is created.
    """
    lines = [
        "[task]",
        f"id = {_toml_str(fields.task_id)}",
        f"objective = {_toml_str(fields.objective)}",
        f"repository = {_toml_str(fields.repository)}",
        "",
    ]
    for check in fields.checks:
        lines.append("[[checks]]")
        lines.append(f"name = {_toml_str(check.name)}")
        lines.append(f"kind = {_toml_str(check.kind)}")
        lines.append(f"argv = {_toml_str_list(check.argv)}")
        lines.append(f"timeout_seconds = {float(check.timeout_seconds)!r}")
        if check.cpu_seconds is not None:
            lines.append(f"cpu_seconds = {check.cpu_seconds}")
        lines.append("")
    acceptance = fields.acceptance
    lines.append("[acceptance]")
    lines.append(f"required = {_toml_str_list(acceptance.required)}")
    lines.append(f"allowed_prefixes = {_toml_str_list(acceptance.allowed_prefixes)}")
    lines.append(f"require_change = {str(acceptance.require_change).lower()}")
    if acceptance.max_changed_files is not None:
        lines.append(f"max_changed_files = {acceptance.max_changed_files}")
    lines.append("")
    sandbox = fields.sandbox
    lines.append("[sandbox]")
    if sandbox.container_image is not None:
        lines.append(f"container_image = {_toml_str(sandbox.container_image)}")
    if sandbox.environment:
        # Keys are quoted too: a raw key containing a newline would inject
        # arbitrary TOML lines (e.g. an extra [[checks]] allowlist entry).
        # Quoted keys stay one token, and load_profile's environment-key
        # validation then fails closed on any key outside its pattern.
        pairs = ", ".join(
            f"{_toml_str(key)} = {_toml_str(value)}"
            for key, value in sorted(sandbox.environment.items())
        )
        lines.append(f"environment = {{ {pairs} }}")
    if sandbox.max_memory_bytes is not None:
        lines.append(f"max_memory_bytes = {sandbox.max_memory_bytes}")
    if sandbox.local_python is not None:
        lines.append(f"local_python = {_toml_str(sandbox.local_python)}")
    lines.append("")
    lines.append("[model]")
    lines.append(f"provider = {_toml_str(fields.model.provider)}")
    if fields.model.name is not None:
        lines.append(f"name = {_toml_str(fields.model.name)}")
    lines.append(f"tier = {_toml_str(fields.model.tier)}")
    lines.append("")
    if fields.approval_required_for:
        lines.append("[approval]")
        lines.append(f"required_for = {_toml_str_list(fields.approval_required_for)}")
        lines.append("")
    _render_budget_section(lines, fields.budget)
    return "\n".join(lines)


def _render_budget_section(lines: list[str], budget: InlineBudgetFields) -> None:
    lines.append("[budget]")
    lines.append(f"max_cost_usd = {float(budget.max_cost_usd)!r}")
    lines.append(f"max_iterations = {budget.max_iterations}")
    if budget.max_total_tokens is not None:
        lines.append(f"max_total_tokens = {budget.max_total_tokens}")
    if budget.max_elapsed_seconds is not None:
        lines.append(f"max_elapsed_seconds = {float(budget.max_elapsed_seconds)!r}")
    if budget.no_progress_limit is not None:
        lines.append(f"no_progress_limit = {budget.no_progress_limit}")
    lines.append("")


def _inline_profile_path(profiles_dir: Path, toml_text: str) -> Path:
    digest = hashlib.sha256(toml_text.encode("utf-8")).hexdigest()[:12]
    return profiles_dir / f"inline-{digest}.toml"


def _validate_inline_toml(toml_text: str, profiles_dir: Path) -> LoopProfile:
    """Persist the inline TOML under the server's profiles dir and validate it.

    Validation runs BEFORE the digest-named file appears: a rejected profile
    must never linger in ``profiles_dir`` (it would show up in the profiles
    listing despite never having created a session). The write goes to a
    sibling tmp file first and is renamed into place only after
    ``load_profile`` accepts it; the digest name makes the rename idempotent
    across restarts.
    """
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = _inline_profile_path(profiles_dir, toml_text)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(toml_text, encoding="utf-8")
    try:
        profile = load_profile(tmp_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(path)
    return profile


def wiring_from_inline(fields: InlineProfileFields, *, profiles_dir: Path) -> SessionWiring:
    """Build validated wiring from inline REST fields (fail closed).

    The fields are rendered into profile TOML and parsed through
    ``load_profile`` — an invalid profile raises ``ProfileError`` and must
    never create a session. The validated TOML text becomes the
    ``profile_source`` so restarts reconstruct the identical profile.
    """
    toml_text = render_inline_profile_toml(fields)
    profile = _validate_inline_toml(toml_text, profiles_dir)
    return SessionWiring(
        objective=profile.task.objective,
        repository=str(profile.repository),
        profile_source={"kind": "inline", "toml": toml_text},
        max_cost_usd=profile.budget.max_cost_usd,
        max_iterations=profile.budget.max_iterations,
        max_total_tokens=profile.budget.max_total_tokens,
        max_elapsed_seconds=profile.budget.max_elapsed_seconds,
        model_provider=profile.model_provider,
        model_name=profile.model_name,
        container_image=profile.container_image,
        created_at=datetime.now(UTC).isoformat(),
    )


def wiring_from_profile_path(path: str | Path) -> SessionWiring:
    """Build validated wiring from an operator-owned profile TOML path."""
    profile = load_profile(path)
    return SessionWiring(
        objective=profile.task.objective,
        repository=str(profile.repository),
        profile_source={"kind": "path", "path": str(path)},
        max_cost_usd=profile.budget.max_cost_usd,
        max_iterations=profile.budget.max_iterations,
        max_total_tokens=profile.budget.max_total_tokens,
        max_elapsed_seconds=profile.budget.max_elapsed_seconds,
        model_provider=profile.model_provider,
        model_name=profile.model_name,
        container_image=profile.container_image,
        created_at=datetime.now(UTC).isoformat(),
    )


# -- Production bundle factory -------------------------------------------------


def _profile_from_wiring(wiring: SessionWiring, profiles_base: Path) -> LoopProfile:
    source = wiring.profile_source
    kind = source.get("kind")
    if kind == "path":
        return load_profile(source["path"])
    if kind == "inline":
        return _validate_inline_toml(source["toml"], profiles_base)
    msg = f"unknown profile source kind: {kind!r}"
    raise ProfileError(msg)


def build_production_bundle_factory(
    profiles_base: Path,
    *,
    ollama_url: str = "http://localhost:11434",
    ollama_context_window: int = 131_072,
) -> BundleFactory:
    """Wire the production bundle factory (profile reload + model adapters).

    The model adapter is mapped from the profile's provider exactly like the
    CLI ``loop`` command: scripted uses the workload's deterministic default,
    ollama/deepseek reuse the CLI's credential-from-environment builders.
    """

    class _ProductionBundleFactory:
        def build(self, wiring: SessionWiring, store: StateStorePort) -> RepairRuntimeBundle:
            profile = _profile_from_wiring(wiring, profiles_base)
            model: ModelPort | None = None
            bundle_owned = False
            try:
                if profile.model_provider == "deepseek":
                    model = build_deepseek_model(profile.task, model_name=profile.model_name)
                elif profile.model_provider == "ollama":
                    model = build_ollama_model(
                        profile.task,
                        model_name=profile.model_name,
                        base_url=ollama_url,
                        context_window_tokens=ollama_context_window,
                    )
                deps = RepairRuntimeDeps(
                    store=store,
                    clock=SystemClock(),
                    sleeper=SystemSleeper(),
                    telemetry=InMemoryTelemetry(),
                    model=model,
                    model_tier=profile.model_tier,
                    budget=profile.budget,
                    no_progress_limit=profile.no_progress_limit,
                )
                bundle = build_adopted_repair_runtime(
                    profile.task,
                    repository=profile.repository,
                    deps=deps,
                    container_image=profile.container_image,
                    environment=profile.environment,
                    limits=profile.limits,
                    approval_required_for=profile.approval_required_for,
                )
                bundle_owned = True
                return bundle
            finally:
                if not bundle_owned and model is not None:
                    close = getattr(model, "close", None)
                    if callable(close):
                        close()

    return _ProductionBundleFactory()
