"""Operator server: REST + WebSocket projection over the durable runtime.

The server is a projection plus command issuer, NEVER a state writer: every
run-state change flows through the :class:`~loopforge.application.runtime.
Runtime` as durable events via the :class:`~loopforge.entrypoints.sessions.
SessionManager`; no route appends events or mutates ``RunState``. Live
updates fan out from the durable store (D6): subscribers receive events only
after they are appended. Driving runs happens on per-run driver threads (D5)
owned by the session manager — never on the event loop.

Trusted-operator local tool (D10): there is deliberately NO authentication;
the CLI binds 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import json
import queue
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from loopforge.adapters.fanout_store import FanOutEventStore
from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.postgres_events import PostgresEventStore
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.application.runtime import UnknownRunError
from loopforge.domain.events import ApprovalRequested, ArtifactRecorded, Event
from loopforge.domain.state import InvalidTransitionError
from loopforge.domain.types import RunId, RunStatus
from loopforge.entrypoints.profile import ProfileError
from loopforge.entrypoints.sessions import (
    BundleFactory,
    InlineAcceptanceFields,
    InlineBudgetFields,
    InlineCheckFields,
    InlineModelFields,
    InlineProfileFields,
    InlineSandboxFields,
    SessionManager,
    SessionRegistry,
    SessionRegistryError,
    SessionStateError,
    UnmanagedRunError,
    build_production_bundle_factory,
    wiring_from_inline,
    wiring_from_profile_path,
)
from loopforge.ports.state_store import StreamVersionConflictError
from loopforge.ports.workspace import WorkspaceError

WS_CLOSE_UNKNOWN_RUN = 4404
_WS_POLL_SECONDS = 0.1


def _next_event(subscriber: queue.Queue[Event]) -> Event:
    """Bounded queue poll so no executor thread ever parks indefinitely.

    An unbounded ``get`` would strand a threadpool thread (and stall event-loop
    shutdown) when a client disconnects while no events flow.
    """
    return subscriber.get(timeout=_WS_POLL_SECONDS)


async def _send_new_events(
    websocket: WebSocket,
    codec: JsonEventCodec,
    events: tuple[Event, ...],
    last_sequence: int,
) -> int:
    """Send every event newer than ``last_sequence``; return the new high-water mark."""
    for event in events:
        if event.sequence <= last_sequence:
            continue
        last_sequence = event.sequence
        await websocket.send_text(codec.encode(event))
    return last_sequence


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerSettings:
    """Composition settings for the operator server."""

    store_kind: str = "postgres"
    """``"postgres"`` (uses ``dsn``) or ``"sqlite"`` (uses ``sqlite_path``)."""
    dsn: str = "postgresql://loopforge:loopforge@127.0.0.1:5432/loopforge"
    sqlite_path: str = ".loopforge/server/events.db"
    data_dir: Path = Path(".loopforge/server")
    """Server-owned state: session registry and inline profiles live here."""
    static_dir: Path | None = None
    """Optional built UI (``ui/dist``) mounted at ``/`` when it exists."""


# -- Request models -------------------------------------------------------------


class InlineCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    argv: list[str]
    timeout_seconds: float
    cpu_seconds: int | None = None


class InlineAcceptanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: list[str]
    allowed_prefixes: list[str]
    require_change: bool = False
    max_changed_files: int | None = None


class InlineSandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_image: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    max_memory_bytes: int | None = None
    local_python: str | None = None


class InlineModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    name: str | None = None
    tier: str


class InlineBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_cost_usd: float
    max_iterations: int
    max_total_tokens: int | None = None
    max_elapsed_seconds: float | None = None
    no_progress_limit: int | None = None


class InlineApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_for: list[str]


class InlineProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    objective: str
    task_id: str | None = None
    checks: list[InlineCheckRequest]
    acceptance: InlineAcceptanceRequest
    sandbox: InlineSandboxRequest = Field(default_factory=InlineSandboxRequest)
    model: InlineModelRequest
    budget: InlineBudgetRequest
    approval: InlineApprovalRequest | None = None


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_path: str | None = None
    inline: InlineProfileRequest | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> CreateSessionRequest:
        if (self.profile_path is None) == (self.inline is None):
            msg = "exactly one of profile_path or inline is required"
            raise ValueError(msg)
        return self


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = "cancelled by operator"


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    reason: str


class InstructionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str
    amend_objective: bool = False


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] | None = None


def _inline_fields(request: InlineProfileRequest) -> InlineProfileFields:
    return InlineProfileFields(
        repository=request.repository,
        objective=request.objective,
        task_id=request.task_id or "server-session",
        checks=tuple(
            InlineCheckFields(
                name=check.name,
                kind=check.kind,
                argv=tuple(check.argv),
                timeout_seconds=check.timeout_seconds,
                cpu_seconds=check.cpu_seconds,
            )
            for check in request.checks
        ),
        acceptance=InlineAcceptanceFields(
            required=tuple(request.acceptance.required),
            allowed_prefixes=tuple(request.acceptance.allowed_prefixes),
            require_change=request.acceptance.require_change,
            max_changed_files=request.acceptance.max_changed_files,
        ),
        sandbox=InlineSandboxFields(
            container_image=request.sandbox.container_image,
            environment=dict(request.sandbox.environment),
            max_memory_bytes=request.sandbox.max_memory_bytes,
            local_python=request.sandbox.local_python,
        ),
        model=InlineModelFields(
            provider=request.model.provider,
            name=request.model.name,
            tier=request.model.tier,
        ),
        budget=InlineBudgetFields(
            max_cost_usd=request.budget.max_cost_usd,
            max_iterations=request.budget.max_iterations,
            max_total_tokens=request.budget.max_total_tokens,
            max_elapsed_seconds=request.budget.max_elapsed_seconds,
            no_progress_limit=request.budget.no_progress_limit,
        ),
        approval_required_for=(
            tuple(request.approval.required_for) if request.approval is not None else ()
        ),
    )


def create_app(  # noqa: PLR0915 - the composition root registers routes linearly
    settings: ServerSettings, *, bundle_factory: BundleFactory | None = None
) -> FastAPI:
    """Build the operator-server ASGI app over a durable event store."""
    codec = JsonEventCodec()
    if settings.store_kind == "sqlite":
        store = FanOutEventStore(SQLiteEventStore(settings.sqlite_path, codec=codec))
    elif settings.store_kind == "postgres":
        store = FanOutEventStore(PostgresEventStore(settings.dsn, codec=codec))
    else:
        msg = f"unknown store kind: {settings.store_kind!r}"
        raise ValueError(msg)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = settings.data_dir / "profiles"
    registry = SessionRegistry(settings.data_dir / "sessions.json")
    manager = SessionManager(
        store,
        registry,
        bundle_factory or build_production_bundle_factory(profiles_dir),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        yield
        manager.shutdown()

    app = FastAPI(title="loopforge operator server", lifespan=lifespan)
    app.state.manager = manager
    app.state.store = store

    # -- error mapping: consistent {"detail": ...} JSON, never tracebacks ----

    async def unknown_run_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    async def conflict_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    async def unprocessable_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    async def registry_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        # Server-side wiring corruption is never the client's fault (so not
        # 4xx) but must still carry the consistent {"detail": ...} shape.
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    app.add_exception_handler(UnknownRunError, unknown_run_handler)
    app.add_exception_handler(UnmanagedRunError, conflict_handler)
    app.add_exception_handler(InvalidTransitionError, conflict_handler)
    app.add_exception_handler(SessionStateError, conflict_handler)
    # A cross-process compare-and-append race (two servers on one store).
    app.add_exception_handler(StreamVersionConflictError, conflict_handler)
    app.add_exception_handler(ProfileError, unprocessable_handler)
    # Workspace checkout/reset failures (empty or unrevertible paths).
    app.add_exception_handler(WorkspaceError, unprocessable_handler)
    app.add_exception_handler(ValueError, unprocessable_handler)
    app.add_exception_handler(SessionRegistryError, registry_error_handler)

    # -- REST: sessions ---------------------------------------------------------

    def list_sessions_route() -> dict[str, object]:
        return {"sessions": manager.list_sessions()}

    def create_session_route(request: CreateSessionRequest) -> dict[str, str]:
        if request.profile_path is not None:
            wiring = wiring_from_profile_path(request.profile_path)
        else:
            assert request.inline is not None
            wiring = wiring_from_inline(_inline_fields(request.inline), profiles_dir=profiles_dir)
        run_id = manager.create_session(wiring)
        return {"run_id": run_id}

    def _session_entry(run_id: str) -> dict[str, object]:
        for entry in manager.list_sessions():
            if entry["run_id"] == run_id:
                return entry
        msg = f"no persisted run: {run_id}"
        raise UnknownRunError(msg)

    def session_detail_route(run_id: str) -> dict[str, object]:
        state = manager.state(run_id)
        entry = _session_entry(run_id)
        wiring = manager.wiring(run_id)
        events = manager.events(run_id)
        pending: dict[str, str] | None = None
        if state.status is RunStatus.WAITING_FOR_APPROVAL:
            requested = [e for e in events if isinstance(e, ApprovalRequested)]
            if requested:
                latest = requested[-1]
                pending = {"action_id": str(latest.action_id), "reason": latest.reason}
        proposal = state.current_proposal
        budget: dict[str, object] | None = None
        if wiring is not None:
            budget = {
                "max_cost_usd": wiring.max_cost_usd,
                "max_iterations": wiring.max_iterations,
                "max_total_tokens": wiring.max_total_tokens,
                "max_elapsed_seconds": wiring.max_elapsed_seconds,
            }
        detail: dict[str, object] = {
            **entry,
            # Objective and status come from the authoritative replay: the
            # runs index is a listing projection and does not track objective
            # amendments (OperatorInstruction events).
            "objective": state.objective,
            "status": state.status.value,
            "budget": budget,
            "iteration": state.iteration,
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "total_tokens": state.total_tokens,
            "last_verification": state.last_verification,
            "last_verification_passed": state.last_verification_passed,
            "last_verification_score": state.last_verification_score,
            "plan": state.plan,
            "current_proposal": (
                {
                    "action_id": str(proposal.action_id),
                    "tool_name": proposal.tool_name,
                    "arguments": dict(proposal.arguments),
                }
                if proposal is not None
                else None
            ),
            "waiting_for_approval": state.status is RunStatus.WAITING_FOR_APPROVAL,
            "pending_approval": pending,
            "last_approval_rejection": state.last_approval_rejection,
            "operator_instructions": list(state.operator_instructions),
            "version": state.version,
            "repository": wiring.repository if wiring is not None else None,
        }
        session_error = manager.session_error(run_id)
        if session_error is not None:
            detail["driver_error"] = f"{type(session_error).__name__}: {session_error}"
        return detail

    def session_events_route(
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict[str, object]:
        events = manager.events(run_id)
        selected = [e for e in events if e.sequence > after_sequence][:limit]
        return {
            "events": [json.loads(codec.encode(event)) for event in selected],
            "latest_sequence": events[-1].sequence,
        }

    def session_artifacts_route(run_id: str) -> dict[str, object]:
        events = manager.events(run_id)
        return {
            "artifacts": [
                {
                    "sequence": event.sequence,
                    "kind": event.kind.value,
                    "label": event.label,
                    "content": event.content,
                    "occurred_at": event.occurred_at.isoformat(),
                }
                for event in events
                if isinstance(event, ArtifactRecorded)
            ]
        }

    def _status_response(run_id: str) -> dict[str, str]:
        return {"run_id": run_id, "status": manager.state(run_id).status.value}

    def start_route(run_id: str) -> dict[str, str]:
        manager.start_driving(RunId(run_id))
        return _status_response(run_id)

    def pause_route(run_id: str) -> dict[str, str]:
        manager.pause(run_id)
        return _status_response(run_id)

    def resume_route(run_id: str) -> dict[str, str]:
        manager.resume(run_id)
        return _status_response(run_id)

    def stop_route(run_id: str, request: StopRequest | None = None) -> dict[str, str]:
        summary = request.summary if request is not None else "cancelled by operator"
        manager.stop(run_id, summary=summary)
        return _status_response(run_id)

    def approve_route(run_id: str, request: ApproveRequest) -> dict[str, str]:
        manager.approve(run_id, request.action_id)
        return _status_response(run_id)

    def reject_route(run_id: str, request: RejectRequest) -> dict[str, str]:
        manager.reject(run_id, request.action_id, request.reason)
        return _status_response(run_id)

    def instructions_route(run_id: str, request: InstructionRequest) -> dict[str, str]:
        manager.instruct(run_id, request.instruction, amend_objective=request.amend_objective)
        return _status_response(run_id)

    def rollback_route(run_id: str, request: RollbackRequest | None = None) -> dict[str, str]:
        paths = tuple(request.paths) if request is not None and request.paths is not None else None
        manager.rollback(run_id, paths)
        return _status_response(run_id)

    def profiles_route() -> dict[str, object]:
        profiles: list[dict[str, str]] = []
        for directory in (profiles_dir, Path.cwd() / ".loopforge"):
            if not directory.is_dir():
                continue
            profiles.extend(
                {"name": path.stem, "path": str(path)} for path in sorted(directory.glob("*.toml"))
            )
        return {"profiles": profiles}

    app.add_api_route("/api/sessions", list_sessions_route, methods=["GET"])
    app.add_api_route("/api/sessions", create_session_route, methods=["POST"], status_code=201)
    app.add_api_route("/api/profiles", profiles_route, methods=["GET"])
    app.add_api_route("/api/sessions/{run_id}", session_detail_route, methods=["GET"])
    app.add_api_route("/api/sessions/{run_id}/events", session_events_route, methods=["GET"])
    app.add_api_route("/api/sessions/{run_id}/artifacts", session_artifacts_route, methods=["GET"])
    app.add_api_route("/api/sessions/{run_id}/start", start_route, methods=["POST"])
    app.add_api_route("/api/sessions/{run_id}/pause", pause_route, methods=["POST"])
    app.add_api_route("/api/sessions/{run_id}/resume", resume_route, methods=["POST"])
    app.add_api_route("/api/sessions/{run_id}/stop", stop_route, methods=["POST"])
    app.add_api_route("/api/sessions/{run_id}/approve", approve_route, methods=["POST"])
    app.add_api_route("/api/sessions/{run_id}/reject", reject_route, methods=["POST"])
    app.add_api_route("/api/sessions/{run_id}/instructions", instructions_route, methods=["POST"])
    app.add_api_route("/api/sessions/{run_id}/rollback", rollback_route, methods=["POST"])

    # -- WebSocket: live event stream --------------------------------------------

    async def session_stream_route(websocket: WebSocket, run_id: str) -> None:
        rid = RunId(run_id)
        await websocket.accept()
        # Unknown runs are closed with a dedicated code AFTER accept: a
        # pre-accept close is translated into an HTTP 403 handshake rejection
        # by real ASGI servers and the code would never reach the client. A
        # run known to the registry but with an empty stream stays open — its
        # events may arrive later.
        if store.current_version(rid) == 0 and registry.get(run_id) is None:
            await websocket.close(code=WS_CLOSE_UNKNOWN_RUN)
            return
        subscriber = store.subscribe(rid)
        try:
            # Subscribe BEFORE reading history so no durable event is missed;
            # live events replayed from history are skipped by sequence. The
            # history read runs on the executor: decoding a long stream must
            # never stall the event loop (and every other client) behind one
            # connecting socket.
            loop = asyncio.get_running_loop()
            history = await loop.run_in_executor(None, store.events_for, rid)
            last_sequence = await _send_new_events(websocket, codec, history, 0)
            receive_task: asyncio.Future[str] | None = None
            try:
                while True:
                    if receive_task is None:
                        receive_task = asyncio.ensure_future(websocket.receive_text())
                    if receive_task.done():
                        # Raises WebSocketDisconnect when the client closed;
                        # a text frame is ignored (this socket streams only).
                        receive_task.result()
                        receive_task = None
                        continue
                    try:
                        event = await loop.run_in_executor(None, _next_event, subscriber)
                    except queue.Empty:
                        continue
                    if event.sequence <= last_sequence:
                        continue
                    if event.sequence > last_sequence + 1:
                        # Cross-process publishes are not ordered against
                        # commits: a skipped sequence would otherwise stay
                        # missing for the life of this connection. Resync
                        # from the durable store instead of skipping forward.
                        missed = await loop.run_in_executor(None, store.events_for, rid)
                        last_sequence = await _send_new_events(
                            websocket, codec, missed, last_sequence
                        )
                        continue
                    last_sequence = event.sequence
                    await websocket.send_text(codec.encode(event))
            finally:
                if receive_task is not None and not receive_task.done():
                    receive_task.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            store.unsubscribe(rid, subscriber)

    app.add_api_websocket_route("/ws/sessions/{run_id}", session_stream_route)

    # Static UI mounts LAST, after every /api and /ws route is registered.
    if settings.static_dir is not None and Path(settings.static_dir).is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(settings.static_dir), html=True),
            name="static",
        )

    return app
