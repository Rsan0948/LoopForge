"""Operator server over FastAPI's in-process TestClient (no network, no PG).

Every run-state change flows through the runtime as durable events; these
tests drive full sessions over HTTP (create → start → approve/reject/stop →
restart rediscovery) against a SQLite store with scripted-model bundles, and
stream live events over the WebSocket fan-out.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.fanout_store import FanOutEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
)
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import OperatorInstruction
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.security import SandboxCapabilities
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import (
    ActionId,
    BudgetLimit,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    WorkspaceId,
)
from loopforge.domain.workspace import WorkspaceStatus
from loopforge.entrypoints.cli import main
from loopforge.entrypoints.repair import RepairRuntimeBundle
from loopforge.entrypoints.server import ServerSettings, create_app
from loopforge.entrypoints.sessions import SessionManager, SessionWiring
from loopforge.ports.sandbox import SandboxCommandResult
from loopforge.ports.state_store import StateStorePort, StreamVersionConflictError
from loopforge.ports.tools import ToolExecutionRequest, ToolResult, UnknownToolError
from loopforge.ports.workspace import WorkspaceError

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _metadata(name: str, approval: ApprovalClass) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.LOCAL_WRITE,
        required_permission=Permission.LOCAL_WRITE,
        side_effect=SideEffectClass.LOCAL_WRITE,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NONE,
        approval=approval,
        timeout_seconds=5.0,
    )


def _proposal(action_id: str, tool_name: str) -> ActionProposal:
    return ActionProposal(ActionId(action_id), tool_name, {"target": "workspace"})


TOOL_METADATA = [
    _metadata("probe", ApprovalClass.NONE),
    _metadata("deploy", ApprovalClass.REQUIRED),
]


class BlockingTools:
    """Tool executor whose execute() blocks until released (deterministic driving tests)."""

    def __init__(self) -> None:
        self._metadata = {item.name: item for item in TOOL_METADATA}
        self.entered = threading.Event()
        self.release = threading.Event()

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        try:
            return self._metadata[tool_name]
        except KeyError as exc:
            msg = f"unknown tool: {tool_name}"
            raise UnknownToolError(msg) from exc

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        self.metadata_for(request.proposal.tool_name)
        self.entered.set()
        self.release.wait(timeout=10)
        return ToolResult(ok=True, observation="all tests pass")


class FakeWorkspace:
    """WorkspacePort-conformant fake recording revert operations."""

    def __init__(self) -> None:
        self.reset_calls = 0
        self.checkout_calls: list[tuple[str, ...]] = []

    @property
    def workspace_id(self) -> WorkspaceId:
        return WorkspaceId("fake-workspace")

    @property
    def root(self) -> Path:
        return Path("/nonexistent-workspace")

    @property
    def base_revision(self) -> str:
        return "base-revision"

    def status(self) -> WorkspaceStatus:
        return WorkspaceStatus()

    def diff(self) -> str:
        return ""

    def checkout(self, paths: tuple[str, ...]) -> None:
        # Mirror the production contract: an empty selection is an error,
        # never a silent no-op.
        if not paths:
            msg = "checkout requires at least one path"
            raise WorkspaceError(msg)
        self.checkout_calls.append(tuple(paths))

    def reset(self) -> None:
        self.reset_calls += 1


class FakeSandbox:
    """SandboxPort-conformant fake (bundle lifecycle only)."""

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            file_api_confined=True,
            symlink_protected=True,
            environment_filtered=True,
            process_timeout=True,
            resource_limits=True,
            output_limited=True,
            process_filesystem_isolated=False,
            network_isolated=False,
            kernel_isolated=False,
        )

    def read_text(self, relative_path: str) -> str:
        del relative_path
        return ""

    def write_text(self, relative_path: str, content: str) -> None:
        del relative_path, content

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        del command_name, timeout_seconds
        return SandboxCommandResult(exit_code=0, stdout="", stderr="", succeeded=True)


@dataclass(slots=True)
class FakeBundleFactory:
    """BundleFactory fake wiring scripted-model runtimes over the app's store."""

    actions: list[ActionProposal]
    results: list[ToolResult]
    tools_override: BlockingTools | None = None
    bundles: list[RepairRuntimeBundle] = field(default_factory=list[RepairRuntimeBundle])
    workspaces: list[FakeWorkspace] = field(default_factory=list[FakeWorkspace])

    def build(self, wiring: SessionWiring, store: StateStorePort) -> RepairRuntimeBundle:
        del wiring  # the fake does not reconstruct profiles
        workspace = FakeWorkspace()
        tools = (
            self.tools_override
            if self.tools_override is not None
            else ScriptedTools(list(self.results), metadata=list(TOOL_METADATA))
        )
        runtime = Runtime(
            model=ScriptedModel(list(self.actions)),
            tools=tools,
            verifier=ObservationContainsVerifier("all tests pass"),
            store=store,
            control=ControlPolicy(BudgetLimit(max_cost_usd=1.0, max_iterations=5)),
            permissions=PermissionPolicy(frozenset({Permission.READ, Permission.LOCAL_WRITE})),
            reliability=ReliabilityPolicy(),
            context=BasicContextBuilder(FixedClock(NOW)),
            clock=FixedClock(NOW),
            sleeper=RecordingSleeper(),
        )
        bundle = RepairRuntimeBundle(runtime=runtime, workspace=workspace, sandbox=FakeSandbox())
        self.bundles.append(bundle)
        self.workspaces.append(workspace)
        return bundle


def _plain_factory() -> FakeBundleFactory:
    return FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        results=[ToolResult(ok=True, observation="all tests pass")],
    )


def _gated_factory() -> FakeBundleFactory:
    return FakeBundleFactory(
        actions=[_proposal("a1", "deploy")],
        results=[ToolResult(ok=True, observation="all tests pass")],
    )


def _gated_then_plain_factory() -> FakeBundleFactory:
    return FakeBundleFactory(
        actions=[_proposal("a1", "deploy"), _proposal("a2", "probe")],
        results=[ToolResult(ok=True, observation="all tests pass")],
    )


def _repo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").touch()
    return root


def _inline_body(repo: Path) -> dict[str, Any]:
    return {
        "inline": {
            "repository": str(repo),
            "objective": "Fix the failing checks.",
            "checks": [
                {
                    "name": "unit_tests",
                    "kind": "TEST",
                    "argv": ["{python}", "-m", "pytest", "-q"],
                    "timeout_seconds": 300,
                }
            ],
            "acceptance": {"required": ["unit_tests"], "allowed_prefixes": ["src", "tests"]},
            "model": {"provider": "scripted", "tier": "economy"},
            "budget": {"max_cost_usd": 5.0, "max_iterations": 30},
        }
    }


def _client(
    tmp_path: Path,
    factory: FakeBundleFactory,
    *,
    data_dir: Path | None = None,
    sqlite_path: Path | None = None,
) -> TestClient:
    settings = ServerSettings(
        store_kind="sqlite",
        sqlite_path=str(sqlite_path or tmp_path / "events.db"),
        data_dir=data_dir or tmp_path / "data",
    )
    return TestClient(create_app(settings, bundle_factory=factory))


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    msg = "condition not met within timeout"
    raise AssertionError(msg)


def _detail(client: TestClient, run_id: str) -> dict[str, Any]:
    response = client.get(f"/api/sessions/{run_id}")
    assert response.status_code == 200
    result: dict[str, Any] = response.json()
    return result


def _wait_for_status(client: TestClient, run_id: str, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        detail = _detail(client, run_id)
        if detail["status"] == status:
            return detail
        time.sleep(0.01)
    msg = f"run did not reach status {status!r}; last: {_detail(client, run_id)!r}"
    raise AssertionError(msg)


def _create(client: TestClient, repo: Path) -> str:
    response = client.post("/api/sessions", json=_inline_body(repo))
    assert response.status_code == 201, response.text
    run_id: str = response.json()["run_id"]
    return run_id


# --- Session creation (allow AND deny) ----------------------------------------


def test_create_session_via_inline_fields(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)

        listing = client.get("/api/sessions").json()
        session = next(s for s in listing["sessions"] if s["run_id"] == run_id)
        assert session["status"] == "ready"
        assert session["driving"] is False
        assert session["managed"] is True
        assert session["objective"] == "Fix the failing checks."

        detail = _detail(client, run_id)
        assert detail["waiting_for_approval"] is False
        assert detail["pending_approval"] is None
        assert detail["budget"]["max_cost_usd"] == 5.0
        assert detail["repository"] == str(repo)
        assert detail["iteration"] == 0


def test_create_session_via_profile_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    profile_path = tmp_path / "profile.toml"
    profile_path.write_text(
        f"""
[task]
id = "path-profile"
objective = "Fix it via path."
repository = "{repo}"

[[checks]]
name = "unit_tests"
kind = "TEST"
argv = ["{{python}}", "-m", "pytest", "-q"]
timeout_seconds = 300

[acceptance]
required = ["unit_tests"]
allowed_prefixes = ["src"]

[model]
provider = "scripted"
tier = "economy"

[budget]
max_cost_usd = 5.0
max_iterations = 30
""",
        encoding="utf-8",
    )
    with _client(tmp_path, _plain_factory()) as client:
        response = client.post("/api/sessions", json={"profile_path": str(profile_path)})
        assert response.status_code == 201, response.text
        detail = _detail(client, response.json()["run_id"])
        assert detail["objective"] == "Fix it via path."

        profiles = client.get("/api/profiles").json()["profiles"]
        assert all(set(entry) == {"name", "path"} for entry in profiles)


def test_create_session_with_unknown_profile_path_is_422(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        response = client.post(
            "/api/sessions", json={"profile_path": str(tmp_path / "missing.toml")}
        )
        assert response.status_code == 422
        assert "detail" in response.json()


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        pytest.param(("budget", "max_cost_usd"), -1.0, id="bad-budget"),
        pytest.param(("checks", "kind"), "BOGUS", id="unknown-check-kind"),
        pytest.param("repository", None, id="missing-repository"),
    ],
)
def test_create_session_rejects_invalid_inline_profiles(
    tmp_path: Path, mutation: Any, value: Any
) -> None:
    repo = _repo(tmp_path / "repo")
    body = _inline_body(repo)
    if mutation == "repository":
        body["inline"]["repository"] = str(tmp_path / "missing")
    elif mutation == ("checks", "kind"):
        body["inline"]["checks"][0]["kind"] = value
    else:
        body["inline"]["budget"]["max_cost_usd"] = value
    with _client(tmp_path, _plain_factory()) as client:
        response = client.post("/api/sessions", json=body)
        assert response.status_code == 422
        assert "detail" in response.json()
        # Fail closed: no session was created.
        assert client.get("/api/sessions").json()["sessions"] == []


def test_create_session_requires_exactly_one_profile_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        neither = client.post("/api/sessions", json={})
        assert neither.status_code == 422

        both = client.post(
            "/api/sessions",
            json={"profile_path": "x.toml", "inline": _inline_body(repo)["inline"]},
        )
        assert both.status_code == 422


# --- Driving to completion -----------------------------------------------------


def test_start_drives_run_to_success_and_exposes_events_and_artifacts(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)

        started = client.post(f"/api/sessions/{run_id}/start")
        assert started.status_code == 200
        assert started.json()["run_id"] == run_id

        detail = _wait_for_status(client, run_id, "succeeded")
        assert detail["driving"] is False
        assert detail["stop_reason"] == "success_verified"
        assert detail["iteration"] == 1
        assert detail["total_tokens"] == detail["input_tokens"] + detail["output_tokens"]
        assert detail["last_verification_passed"] is True

        events = client.get(f"/api/sessions/{run_id}/events").json()
        assert events["latest_sequence"] == len(events["events"])
        event_types = [event["event_type"] for event in events["events"]]
        assert event_types[0] == "RunStarted"
        assert "RunStopped" in event_types
        assert [event["event"]["sequence"] for event in events["events"]] == list(
            range(1, events["latest_sequence"] + 1)
        )

        tail = client.get(f"/api/sessions/{run_id}/events?after_sequence=2&limit=2").json()
        assert [event["event"]["sequence"] for event in tail["events"]] == [3, 4]
        assert tail["latest_sequence"] == events["latest_sequence"]

        artifacts = client.get(f"/api/sessions/{run_id}/artifacts").json()
        assert artifacts == {"artifacts": []}


# --- Approval round-trips (allow AND deny) --------------------------------------


def test_approval_round_trip_through_http(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")

        waiting = _wait_for_status(client, run_id, "waiting_for_approval")
        assert waiting["waiting_for_approval"] is True
        assert waiting["pending_approval"]["action_id"] == "a1"
        assert "deploy" in waiting["pending_approval"]["reason"]
        assert waiting["current_proposal"]["tool_name"] == "deploy"
        assert waiting["current_proposal"]["action_id"] == "a1"

        approved = client.post(f"/api/sessions/{run_id}/approve", json={"action_id": "a1"})
        assert approved.status_code == 200

        detail = _wait_for_status(client, run_id, "succeeded")
        assert detail["waiting_for_approval"] is False
        event_types = [
            event["event_type"]
            for event in client.get(f"/api/sessions/{run_id}/events").json()["events"]
        ]
        assert "ApprovalRequested" in event_types
        assert "ApprovalGranted" in event_types


def test_approve_with_wrong_action_id_is_409(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "waiting_for_approval")

        denied = client.post(f"/api/sessions/{run_id}/approve", json={"action_id": "nope"})
        assert denied.status_code == 409
        assert "not waiting on action" in denied.json()["detail"]
        assert _detail(client, run_id)["status"] == "waiting_for_approval"


def test_approve_a_non_waiting_run_is_409(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)  # created but never started: READY, not waiting

        denied = client.post(f"/api/sessions/{run_id}/approve", json={"action_id": "a1"})
        assert denied.status_code == 409
        assert "not waiting for approval" in denied.json()["detail"]


def test_reject_round_trip_replans_and_records_the_reason(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_then_plain_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "waiting_for_approval")

        rejected = client.post(
            f"/api/sessions/{run_id}/reject",
            json={"action_id": "a1", "reason": "too risky"},
        )
        assert rejected.status_code == 200

        detail = _wait_for_status(client, run_id, "succeeded")
        assert detail["last_approval_rejection"] == "too risky"
        event_types = [
            event["event_type"]
            for event in client.get(f"/api/sessions/{run_id}/events").json()["events"]
        ]
        assert "ApprovalRejected" in event_types


def test_reject_with_wrong_action_id_is_409(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "waiting_for_approval")

        denied = client.post(
            f"/api/sessions/{run_id}/reject",
            json={"action_id": "nope", "reason": "no"},
        )
        assert denied.status_code == 409


# --- Operator instructions --------------------------------------------------------


def test_instruction_can_amend_the_objective_of_a_waiting_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "waiting_for_approval")

        response = client.post(
            f"/api/sessions/{run_id}/instructions",
            json={"instruction": "only touch adder.py", "amend_objective": True},
        )
        assert response.status_code == 200

        detail = _detail(client, run_id)
        assert detail["objective"] == "only touch adder.py"
        assert detail["operator_instructions"] == ["only touch adder.py"]
        assert detail["status"] == "waiting_for_approval"


def test_instruction_on_a_terminal_run_is_409(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "succeeded")

        denied = client.post(
            f"/api/sessions/{run_id}/instructions",
            json={"instruction": "too late"},
        )
        assert denied.status_code == 409
        assert "terminal" in denied.json()["detail"]


# --- Stop / pause -----------------------------------------------------------------


def test_stop_cancels_a_run_parked_at_the_approval_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "waiting_for_approval")

        stopped = client.post(f"/api/sessions/{run_id}/stop", json={"summary": "operator halt"})
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "cancelled"

        detail = _detail(client, run_id)
        assert detail["status"] == "cancelled"
        assert detail["stop_reason"] == "cancelled"
        stop_events = [
            event
            for event in client.get(f"/api/sessions/{run_id}/events").json()["events"]
            if event["event_type"] == "RunStopped"
        ]
        assert stop_events[-1]["event"]["summary"] == "operator halt"


def test_stop_without_a_body_uses_the_default_summary(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)
        stopped = client.post(f"/api/sessions/{run_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "cancelled"


def test_pause_is_an_idempotent_noop_on_a_quiescent_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "waiting_for_approval")

        for _ in range(2):
            paused = client.post(f"/api/sessions/{run_id}/pause")
            assert paused.status_code == 200
            assert paused.json()["status"] == "waiting_for_approval"


# --- Rollback ------------------------------------------------------------------------


def test_rollback_reset_and_selective_checkout(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    factory = _plain_factory()
    with _client(tmp_path, factory) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "succeeded")

        reset = client.post(f"/api/sessions/{run_id}/rollback", json={})
        assert reset.status_code == 200
        assert factory.workspaces[-1].reset_calls == 1

        selective = client.post(f"/api/sessions/{run_id}/rollback", json={"paths": ["src/a.py"]})
        assert selective.status_code == 200
        assert factory.workspaces[-1].checkout_calls == [("src/a.py",)]

        no_body = client.post(f"/api/sessions/{run_id}/rollback")
        assert no_body.status_code == 200
        assert factory.workspaces[-1].reset_calls == 2


# --- Restart rediscovery ---------------------------------------------------------------


def test_restart_rediscovery_resumes_from_store_and_registry(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    data_dir = tmp_path / "data"
    sqlite_path = tmp_path / "events.db"

    with _client(
        tmp_path, _plain_factory(), data_dir=data_dir, sqlite_path=sqlite_path
    ) as client_a:
        finished = _create(client_a, repo)
        client_a.post(f"/api/sessions/{finished}/start")
        _wait_for_status(client_a, finished, "succeeded")
        parked = _create(client_a, repo)  # never started: non-terminal

    # A SECOND app instance over the same store + registry simulates a restart.
    with _client(
        tmp_path, _plain_factory(), data_dir=data_dir, sqlite_path=sqlite_path
    ) as client_b:
        listing = client_b.get("/api/sessions").json()["sessions"]
        by_id = {entry["run_id"]: entry for entry in listing}
        assert by_id[finished]["status"] == "succeeded"
        assert by_id[finished]["managed"] is True
        assert by_id[finished]["driving"] is False
        assert by_id[parked]["status"] == "ready"

        detail = _detail(client_b, finished)
        assert detail["status"] == "succeeded"
        assert detail["budget"]["max_iterations"] == 30

        resumed = client_b.post(f"/api/sessions/{parked}/resume")
        assert resumed.status_code == 200
        _wait_for_status(client_b, parked, "succeeded")


def test_store_run_absent_from_the_registry_is_unmanaged(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    sqlite_path = tmp_path / "events.db"

    with _client(tmp_path, _plain_factory(), sqlite_path=sqlite_path) as client_a:
        run_id = _create(client_a, repo)

    # Same store, but a fresh data dir: the run is in the store yet unknown
    # to this server's registry.
    with _client(
        tmp_path,
        _plain_factory(),
        data_dir=tmp_path / "other-data",
        sqlite_path=sqlite_path,
    ) as client_b:
        listing = client_b.get("/api/sessions").json()["sessions"]
        assert [(entry["run_id"], entry["managed"]) for entry in listing] == [(run_id, False)]

        detail = _detail(client_b, run_id)
        assert detail["budget"] is None
        assert detail["repository"] is None

        cases: list[tuple[str, dict[str, Any] | None]] = [
            ("resume", None),
            ("start", None),
            ("stop", None),
            ("rollback", {}),
        ]
        for endpoint, body in cases:
            response = client_b.post(f"/api/sessions/{run_id}/{endpoint}", json=body)
            assert response.status_code == 409, endpoint
            assert "not managed" in response.json()["detail"]


# --- WebSocket streaming -----------------------------------------------------------------


def test_websocket_streams_history_then_live_events(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "waiting_for_approval")

        history_count = client.get(f"/api/sessions/{run_id}/events").json()["latest_sequence"]

        with client.websocket_connect(f"/ws/sessions/{run_id}") as websocket:
            history = [json.loads(websocket.receive_text()) for _ in range(history_count)]
            assert [frame["event"]["sequence"] for frame in history] == list(
                range(1, history_count + 1)
            )
            assert history[0]["event_type"] == "RunStarted"
            assert history[-1]["event_type"] == "ApprovalRequested"
            assert all(frame["schema_version"] == 1 for frame in history)

            # Client text frames are ignored (the socket streams only), and
            # idle queue polls must not disturb the stream.
            websocket.send_text("ignored")
            time.sleep(0.3)
            client.post(f"/api/sessions/{run_id}/approve", json={"action_id": "a1"})

            live_types: list[str] = []
            deadline = time.monotonic() + 10.0
            while "RunStopped" not in live_types and time.monotonic() < deadline:
                frame = json.loads(websocket.receive_text())
                live_types.append(frame["event_type"])
            assert live_types[0] == "ApprovalGranted"
            assert "RunStopped" in live_types


def test_detail_surfaces_driver_errors(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    factory = FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        # The verifier never passes and the scripted model exhausts: the
        # driver records the failure instead of crashing silently.
        results=[ToolResult(ok=True, observation="still failing")],
    )
    with _client(tmp_path, factory) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")

        _wait_for(lambda: "driver_error" in _detail(client, run_id))
        detail = _detail(client, run_id)
        assert "scripted model exhausted" in detail["driver_error"]
        assert detail["driving"] is False


def test_websocket_rejects_an_unknown_run_with_4404(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        # The close lands after accept so the code reaches real ASGI clients.
        with (
            client.websocket_connect("/ws/sessions/run_missing") as websocket,
            pytest.raises(WebSocketDisconnect) as exc_info,
        ):
            websocket.receive_text()
        assert exc_info.value.code == 4404


# --- Unknown runs ------------------------------------------------------------------------


def test_unknown_run_maps_to_404_on_every_endpoint(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        for get_url in (
            "/api/sessions/run_missing",
            "/api/sessions/run_missing/events",
            "/api/sessions/run_missing/artifacts",
        ):
            response = client.get(get_url)
            assert response.status_code == 404, get_url
            assert "detail" in response.json()

        for post_url, body in (
            ("/api/sessions/run_missing/start", None),
            ("/api/sessions/run_missing/pause", None),
            ("/api/sessions/run_missing/resume", None),
            ("/api/sessions/run_missing/stop", None),
            ("/api/sessions/run_missing/approve", {"action_id": "a1"}),
            ("/api/sessions/run_missing/reject", {"action_id": "a1", "reason": "no"}),
            ("/api/sessions/run_missing/instructions", {"instruction": "hi"}),
            ("/api/sessions/run_missing/rollback", {}),
        ):
            response = client.post(post_url, json=body)
            assert response.status_code == 404, post_url
            assert "detail" in response.json()


# --- CLI: serve command ---------------------------------------------------------------------


def _argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["loopforge", *args])


def test_serve_help_documents_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "serve", "--help")

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--port", "--host", "--dsn", "--sqlite", "--data-dir", "--static-dir"):
        assert flag in out


def test_serve_sqlite_wires_settings_and_invokes_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def _fake_run(app: Any, *, host: str, port: int, **kwargs: Any) -> None:
        captured.update(app=app, host=host, port=port)

    monkeypatch.setattr("uvicorn.run", _fake_run)
    _argv(
        monkeypatch,
        "serve",
        "--sqlite",
        str(tmp_path / "events.db"),
        "--data-dir",
        str(tmp_path / "data"),
        "--port",
        "9000",
    )

    assert main() == 0
    assert isinstance(captured["app"], FastAPI)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9000


def test_serve_defaults_to_postgres_on_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def _fake_create_app(settings: ServerSettings, **kwargs: Any) -> object:
        captured["settings"] = settings
        return object()

    def _fake_run(app: Any, *, host: str, port: int, **kwargs: Any) -> None:
        captured.update(host=host, port=port)

    monkeypatch.setattr("loopforge.entrypoints.server.create_app", _fake_create_app)
    monkeypatch.setattr("uvicorn.run", _fake_run)
    _argv(monkeypatch, "serve", "--data-dir", str(tmp_path / "data"))

    assert main() == 0
    settings = captured["settings"]
    assert settings.store_kind == "postgres"
    assert settings.dsn.startswith("postgresql://")
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123


def test_serve_warns_on_an_off_loopback_bind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_run(app: Any, *, host: str, port: int, **kwargs: Any) -> None:
        del app, host, port, kwargs

    monkeypatch.setattr("uvicorn.run", _fake_run)
    _argv(
        monkeypatch,
        "serve",
        "--sqlite",
        str(tmp_path / "events.db"),
        "--data-dir",
        str(tmp_path / "data"),
        "--host",
        "0.0.0.0",  # deliberate: exercises the off-loopback warning
    )

    assert main() == 0
    assert "NO authentication" in capsys.readouterr().err

    # The loopback default stays silent.
    _argv(
        monkeypatch,
        "serve",
        "--sqlite",
        str(tmp_path / "events.db"),
        "--data-dir",
        str(tmp_path / "data"),
    )
    assert main() == 0
    assert capsys.readouterr().err == ""


def test_create_app_rejects_an_unknown_store_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown store kind"):
        create_app(
            ServerSettings(
                store_kind="bogus",
                sqlite_path=str(tmp_path / "events.db"),
                data_dir=tmp_path / "data",
            )
        )


# --- Hardening: error mapping, request bounds, WS resync -------------------------


def test_rollback_with_empty_paths_is_422(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)

        denied = client.post(f"/api/sessions/{run_id}/rollback", json={"paths": []})

        assert denied.status_code == 422
        assert "at least one path" in denied.json()["detail"]


def test_cas_conflict_maps_to_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-process compare-and-append loss is a conflict, never a 500."""
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)
        manager = cast("SessionManager", cast("FastAPI", client.app).state.manager)

        def racing_approve(_run_id: object, _action_id: object) -> None:
            raise StreamVersionConflictError(RunId(run_id), expected=3, actual=4)

        monkeypatch.setattr(manager, "approve", racing_approve)

        response = client.post(f"/api/sessions/{run_id}/approve", json={"action_id": "a1"})

        assert response.status_code == 409
        assert "version conflict" in response.json()["detail"]


def test_corrupt_registry_maps_to_500_with_detail(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    data_dir = tmp_path / "data"
    with _client(tmp_path, _plain_factory(), data_dir=data_dir) as client:
        _create(client, repo)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "sessions.json").write_text("{not json", encoding="utf-8")

        response = client.get("/api/sessions")

        assert response.status_code == 500
        assert "not valid JSON" in response.json()["detail"]


def test_events_route_bounds_are_enforced(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)

        assert client.get(f"/api/sessions/{run_id}/events?limit=0").status_code == 422
        assert client.get(f"/api/sessions/{run_id}/events?limit=5001").status_code == 422
        assert client.get(f"/api/sessions/{run_id}/events?after_sequence=-1").status_code == 422
        assert client.get(f"/api/sessions/{run_id}/events?limit=1").status_code == 200


def test_blank_instruction_is_422(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _gated_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "waiting_for_approval")

        for blank in ("", "   "):
            denied = client.post(
                f"/api/sessions/{run_id}/instructions",
                json={"instruction": blank, "amend_objective": True},
            )
            assert denied.status_code == 422
            assert "must not be blank" in denied.json()["detail"]

        # The objective survives: no blanking event was persisted.
        assert _detail(client, run_id)["objective"] != ""
        allowed = client.post(
            f"/api/sessions/{run_id}/instructions",
            json={"instruction": "focus on the adder"},
        )
        assert allowed.status_code == 200


def test_failed_inline_profile_is_not_persisted(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    data_dir = tmp_path / "data"
    with _client(tmp_path, _plain_factory(), data_dir=data_dir) as client:
        body = _inline_body(repo)
        body["inline"]["acceptance"]["allowed_prefixes"] = ["../outside"]

        denied = client.post("/api/sessions", json=body)

        assert denied.status_code == 422
        profiles_dir = data_dir / "profiles"
        orphan_profiles = list(profiles_dir.glob("inline-*.toml")) if profiles_dir.is_dir() else []
        assert orphan_profiles == []
        assert not any(
            entry["name"].startswith("inline-")
            for entry in client.get("/api/profiles").json()["profiles"]
        )


def test_start_while_driving_and_start_on_terminal_are_409(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tools = BlockingTools()
    factory = FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        results=[ToolResult(ok=True, observation="all tests pass")],
        tools_override=tools,
    )
    with _client(tmp_path, factory) as client:
        run_id = _create(client, repo)
        assert client.post(f"/api/sessions/{run_id}/start").status_code == 200
        assert tools.entered.wait(timeout=5)

        driving = client.post(f"/api/sessions/{run_id}/start")
        assert driving.status_code == 409
        assert "already driving" in driving.json()["detail"]

        tools.release.set()
        _wait_for_status(client, run_id, "succeeded")

        terminal = client.post(f"/api/sessions/{run_id}/start")
        assert terminal.status_code == 409
        assert "terminal" in terminal.json()["detail"]


def test_websocket_resyncs_when_a_live_frame_skips_a_sequence(tmp_path: Path) -> None:
    """Cross-process publishes are unordered: a skipped sequence must be healed.

    A commit from another process advances the durable stream without touching
    this server's subscriber queues; the next published frame then arrives with
    a sequence gap. The consumer must resync from the durable store instead of
    dropping the missed sequence for the life of the connection.
    """
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)  # stream: RunStarted(1), PlanCreated(2)
        rid = RunId(run_id)
        app_store = cast("FanOutEventStore", cast("FastAPI", client.app).state.store)
        delegate: StateStorePort = app_store._delegate  # pyright: ignore[reportPrivateUsage]

        with client.websocket_connect(f"/ws/sessions/{run_id}") as websocket:
            history = [json.loads(websocket.receive_text()) for _ in range(2)]
            assert [frame["event"]["sequence"] for frame in history] == [1, 2]

            # An append that bypasses this process's fan-out publish.
            delegate.append(
                OperatorInstruction(
                    event_id=EventId("evt-off-channel"),
                    run_id=rid,
                    occurred_at=NOW,
                    sequence=3,
                    instruction="off-channel",
                    amends_objective=False,
                ),
                expected_version=2,
            )
            # The next published frame arrives as sequence 4: a gap.
            app_store.append(
                OperatorInstruction(
                    event_id=EventId("evt-on-channel"),
                    run_id=rid,
                    occurred_at=NOW,
                    sequence=4,
                    instruction="on-channel",
                    amends_objective=False,
                ),
                expected_version=3,
            )

            frames = [json.loads(websocket.receive_text()) for _ in range(2)]

            assert [frame["event"]["sequence"] for frame in frames] == [3, 4]
            assert [frame["event"]["instruction"] for frame in frames] == [
                "off-channel",
                "on-channel",
            ]
