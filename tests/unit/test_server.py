"""Operator server over FastAPI's in-process TestClient (no network, no PG).

Every run-state change flows through the runtime as durable events; these
tests drive full sessions over HTTP (create → start → approve/reject/stop →
restart rediscovery) against a SQLite store with scripted-model bundles, and
stream live events over the WebSocket fan-out.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
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
from loopforge.domain.benchmarks import BenchmarkReport, ConfigReport
from loopforge.domain.events import OperatorInstruction
from loopforge.domain.policies import (
    ContextAllocationBounds,
    ExecutionPolicy,
    PolicyLifecycle,
    PolicyRecord,
)
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
from loopforge.entrypoints import fsbrowse
from loopforge.entrypoints.cli import main
from loopforge.entrypoints.eval import EvalReportStore, report_to_dict
from loopforge.entrypoints.policy import PolicyRegistryStore, policy_record_to_dict
from loopforge.entrypoints.repair import RepairRuntimeBundle
from loopforge.entrypoints.server import ServerSettings, create_app
from loopforge.entrypoints.sessions import SessionManager, SessionWiring
from loopforge.ports.sandbox import SandboxCommandResult
from loopforge.ports.state_store import StateStorePort, StreamVersionConflictError
from loopforge.ports.tools import ToolExecutionRequest, ToolResult, UnknownToolError
from loopforge.ports.workspace import WorkspaceError
from loopforge.workloads.benchmarks import (
    BENCHMARK_SUITE_VERSION,
    benchmark_content_lock,
    benchmark_suite,
)

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


def _client(  # noqa: PLR0913 - test client helper keeps every setting explicit
    tmp_path: Path,
    factory: FakeBundleFactory,
    *,
    data_dir: Path | None = None,
    sqlite_path: Path | None = None,
    evals_dir: Path | None = None,
    policies_dir: Path | None = None,
) -> TestClient:
    settings = ServerSettings(
        store_kind="sqlite",
        sqlite_path=str(sqlite_path or tmp_path / "events.db"),
        data_dir=data_dir or tmp_path / "data",
        evals_dir=evals_dir,
        policies_dir=policies_dir,
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


def test_create_session_inline_round_trips_no_progress_limit(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    data_dir = tmp_path / "data"
    body = _inline_body(repo)
    body["inline"]["budget"]["no_progress_limit"] = 9
    with _client(tmp_path, _plain_factory(), data_dir=data_dir) as client:
        response = client.post("/api/sessions", json=body)
        assert response.status_code == 201, response.text

    persisted = list((data_dir / "profiles").glob("inline-*.toml"))
    assert len(persisted) == 1
    assert "no_progress_limit = 9" in persisted[0].read_text(encoding="utf-8")


def test_create_session_inline_round_trips_container_image(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    data_dir = tmp_path / "data"
    body = _inline_body(repo)
    body["inline"]["sandbox"] = {"container_image": "python:3.12-alpine"}
    # Container mode: {python} is not substituted; argv must use the
    # in-container interpreter path.
    body["inline"]["checks"][0]["argv"] = ["/usr/local/bin/python", "-m", "pytest", "-q"]
    with _client(tmp_path, _plain_factory(), data_dir=data_dir) as client:
        response = client.post("/api/sessions", json=body)
        assert response.status_code == 201, response.text

    persisted = list((data_dir / "profiles").glob("inline-*.toml"))
    assert len(persisted) == 1
    assert 'container_image = "python:3.12-alpine"' in persisted[0].read_text(encoding="utf-8")


# --- Follow-up (PACS-014b) -----------------------------------------------------


def test_follow_up_creates_a_successor_session(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/start")
        _wait_for_status(client, run_id, "succeeded")

        response = client.post(f"/api/sessions/{run_id}/follow-up")
        assert response.status_code == 201, response.text
        successor_id = response.json()["run_id"]
        assert successor_id != run_id

        detail = _detail(client, successor_id)
        assert detail["status"] == "ready"
        assert detail["objective"].startswith("Fix the failing checks.")
        assert f"Follow-up report from run {run_id}" in detail["objective"]
        assert detail["repository"] == str(repo)


def test_follow_up_is_denied_for_non_terminal_and_unknown_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)

        live = client.post(f"/api/sessions/{run_id}/follow-up")
        assert live.status_code == 409, live.text
        assert "terminal" in live.json()["detail"]

        missing = client.post("/api/sessions/run_missing/follow-up")
        assert missing.status_code == 404, missing.text


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


# --- force-release (PACS-015) ---------------------------------------------------


def test_force_release_stops_a_zombie_run_over_http(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)

        response = client.post(
            f"/api/sessions/{run_id}/force-release",
            json={"summary": "operator drill", "confirm": True},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"run_id": run_id, "status": "force-released"}
        detail = _detail(client, run_id)
        assert detail["status"] == "cancelled"
        stop_events = [
            event
            for event in client.get(f"/api/sessions/{run_id}/events").json()["events"]
            if event["event_type"] == "RunStopped"
        ]
        assert stop_events[-1]["event"]["summary"] == "operator drill"
        # The repository claim is released: a fresh session adopts the checkout.
        successor = client.post("/api/sessions", json=_inline_body(repo))
        assert successor.status_code == 201, successor.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"summary": "no confirmation"},
        {"confirm": False},
        {"confirm": "true"},  # string coercion is not a confirmation
        {"confirm": 1},
    ],
)
def test_force_release_requires_literal_confirmation(
    tmp_path: Path, body: dict[str, object]
) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)

        response = client.post(f"/api/sessions/{run_id}/force-release", json=body)

        assert response.status_code == 422, response.text
        assert _detail(client, run_id)["status"] == "ready"


def test_force_release_denies_an_unknown_run_over_http(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        response = client.post("/api/sessions/run_ghost/force-release", json={"confirm": True})

        assert response.status_code == 404, response.text


def test_force_release_denies_a_terminal_run_over_http(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _create(client, repo)
        client.post(f"/api/sessions/{run_id}/stop")

        response = client.post(f"/api/sessions/{run_id}/force-release", json={"confirm": True})

        assert response.status_code == 409, response.text
        assert "already terminal" in response.json()["detail"]


# --- provenance / explain / lineage (PACS-015) ----------------------------------


def _driven_run(client: TestClient, repo: Path) -> str:
    run_id = _create(client, repo)
    client.post(f"/api/sessions/{run_id}/start")
    _wait_for_status(client, run_id, "succeeded")
    return run_id


def test_provenance_route_serves_the_derived_graph_deterministically(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _driven_run(client, repo)

        first = client.get(f"/api/sessions/{run_id}/provenance")
        assert first.status_code == 200, first.text
        graph = first.json()
        assert graph["run_id"] == run_id
        node_ids = [node["node_id"] for node in graph["nodes"]]
        assert len(node_ids) == len(set(node_ids))
        # One node per durable event, in stream order.
        events = client.get(f"/api/sessions/{run_id}/events").json()["events"]
        assert len(graph["nodes"]) == len(events)
        assert [node["sequence"] for node in graph["nodes"]] == [
            event["event"]["sequence"] for event in events
        ]
        kinds = {node["kind"] for node in graph["nodes"]}
        assert {"requirement", "model_turn", "action", "verification"} <= kinds
        known = set(node_ids)
        for edge in graph["edges"]:
            assert edge["source_id"] in known
            assert edge["target_id"] in known
        # Pure projection: the same stream always yields the identical graph.
        second = client.get(f"/api/sessions/{run_id}/provenance")
        assert second.json() == graph


def test_provenance_route_denies_an_unknown_run(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        response = client.get("/api/sessions/run_ghost/provenance")
        assert response.status_code == 404, response.text


def test_explain_route_answers_why_a_node_happened(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _driven_run(client, repo)
        graph = client.get(f"/api/sessions/{run_id}/provenance").json()
        action = next(node for node in graph["nodes"] if node["kind"] == "action")

        response = client.get(f"/api/sessions/{run_id}/explain", params={"node": action["node_id"]})

        assert response.status_code == 200, response.text
        explanation = response.json()
        assert explanation["run_id"] == run_id
        assert explanation["node"]["node_id"] == action["node_id"]
        # The causal spine reaches back to the requirement, oldest first.
        assert explanation["chain"][-1]["sequence"] < explanation["node"]["sequence"]
        assert explanation["chain"][0]["kind"] == "requirement"
        sequences = [item["sequence"] for item in explanation["chain"]]
        assert sequences == sorted(sequences)


def test_explain_route_denies_unknown_nodes_and_missing_params(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        run_id = _driven_run(client, repo)

        unknown = client.get(f"/api/sessions/{run_id}/explain", params={"node": "action:999"})
        assert unknown.status_code == 404, unknown.text
        assert "unknown provenance node" in unknown.json()["detail"]

        missing = client.get(f"/api/sessions/{run_id}/explain")
        assert missing.status_code == 422, missing.text


def test_lineage_route_links_follow_up_runs_in_both_directions(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    with _client(tmp_path, _plain_factory()) as client:
        root = _driven_run(client, repo)
        follow = client.post(f"/api/sessions/{root}/follow-up")
        assert follow.status_code == 201, follow.text
        child = follow.json()["run_id"]

        child_lineage = client.get(f"/api/sessions/{child}/lineage")
        assert child_lineage.status_code == 200, child_lineage.text
        assert child_lineage.json() == {
            "run_id": child,
            "parent_run_id": root,
            "ancestors": [root],
            "children": [],
        }

        root_lineage = client.get(f"/api/sessions/{root}/lineage")
        assert root_lineage.status_code == 200, root_lineage.text
        assert root_lineage.json() == {
            "run_id": root,
            "parent_run_id": None,
            "ancestors": [],
            "children": [child],
        }


def test_lineage_route_denies_an_unknown_run(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        response = client.get("/api/sessions/run_ghost/lineage")
        assert response.status_code == 404, response.text


def _corrupt_payload(
    sqlite_path: Path, event_type: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    # Corruption is exactly what the append-only trigger exists to prevent,
    # so simulating bit-rot has to drop it first (restored afterwards).
    connection = sqlite3.connect(sqlite_path)
    try:
        row = connection.execute(
            "SELECT payload FROM events WHERE event_type = ? LIMIT 1", (event_type,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        mutate(payload)
        connection.execute("DROP TRIGGER events_are_append_only_update")
        connection.execute(
            "UPDATE events SET payload = ? WHERE event_type = ?",
            (json.dumps(payload), event_type),
        )
        connection.execute(
            """
            CREATE TRIGGER events_are_append_only_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events are append-only');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_provenance_route_fails_closed_with_detail_shape_on_a_corrupted_stream(
    tmp_path: Path,
) -> None:
    # A mistyped stored field is server-side corruption: the read route fails
    # closed with the consistent {"detail": ...} 500 shape — never a bare
    # plain-text 500, and never a 422 blaming a parameterless GET.
    repo = _repo(tmp_path / "repo")
    sqlite_path = tmp_path / "events.db"
    with _client(tmp_path, _plain_factory(), sqlite_path=sqlite_path) as client:
        run_id = _driven_run(client, repo)
        _corrupt_payload(sqlite_path, "RunStarted", lambda p: p["event"].update(objective=42))

        response = client.get(f"/api/sessions/{run_id}/provenance")

        assert response.status_code == 500, response.text
        assert "detail" in response.json()


def test_provenance_route_maps_unknown_event_types_to_500_not_422(tmp_path: Path) -> None:
    # UnknownEventTypeError is a ValueError subclass: the explicit 500
    # registration must win over the generic ValueError -> 422 mapping.
    repo = _repo(tmp_path / "repo")
    sqlite_path = tmp_path / "events.db"
    with _client(tmp_path, _plain_factory(), sqlite_path=sqlite_path) as client:
        run_id = _driven_run(client, repo)
        _corrupt_payload(sqlite_path, "PlanCreated", lambda p: p.update(event_type="Ghost"))

        response = client.get(f"/api/sessions/{run_id}/provenance")

        assert response.status_code == 500, response.text
        assert "unknown event type" in response.json()["detail"]


# --- Benchmark suite + eval report exposure (PACS-016, M7) ---------------------

EVAL_LOCK_HASH = "0123456789abcdef" * 4


def _eval_entry(  # noqa: PLR0913 - report fixture helper keeps every field explicit
    config_id: str,
    task_id: str,
    *,
    trials: int = 2,
    successes: int = 1,
    false_successes: int = 0,
    mean_cost_usd: float = 0.02,
    mean_latency_seconds: float = 1.5,
    mean_total_tokens: float = 240.0,
    mean_human_interventions: float = 0.0,
) -> ConfigReport:
    return ConfigReport(
        config_id=config_id,
        task_id=task_id,
        trials=trials,
        successes=successes,
        false_successes=false_successes,
        success_rate=successes / trials,
        false_success_rate=false_successes / trials,
        mean_cost_usd=mean_cost_usd,
        mean_latency_seconds=mean_latency_seconds,
        mean_total_tokens=mean_total_tokens,
        mean_human_interventions=mean_human_interventions,
    )


def _eval_report(report_id: str = "eval-report-1") -> BenchmarkReport:
    return BenchmarkReport(
        report_id=report_id,
        suite_version=BENCHMARK_SUITE_VERSION,
        lock_hash=EVAL_LOCK_HASH,
        config_reports=(
            _eval_entry("baseline", "bench-transient-api", successes=2),
            _eval_entry(
                "baseline",
                "bench-provider-outage",
                successes=0,
                mean_cost_usd=0.0,
                mean_total_tokens=0.0,
                mean_latency_seconds=0.25,
            ),
            _eval_entry("tight-budget", "bench-transient-api", false_successes=1),
        ),
        pareto_config_ids=("baseline",),
    )


def test_benchmark_suite_route_exposes_the_locked_definition(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        response = client.get("/api/benchmark/suite")

        assert response.status_code == 200, response.text
        body = response.json()
        suite = benchmark_suite()
        # The operator-visible proof the benchmark is locked: both hashes are
        # computed from the code-owned definitions on every request.
        assert body["version"] == BENCHMARK_SUITE_VERSION == suite.version
        assert body["lock_hash"] == suite.lock_hash
        assert body["content_lock"] == benchmark_content_lock()
        assert [task["task_id"] for task in body["tasks"]] == [task.task_id for task in suite.tasks]
        assert len(body["tasks"]) == 12
        for task in body["tasks"]:
            assert set(task) == {
                "task_id",
                "category",
                "sandbox_mode",
                "live_eligible",
                "grader_ids",
            }
        transient = next(t for t in body["tasks"] if t["task_id"] == "bench-transient-api")
        assert transient["category"] == "transient_api"
        assert transient["sandbox_mode"] == "trusted_local"
        assert transient["live_eligible"] is False
        assert transient["grader_ids"] == ["verified_success", "scope_discipline", "recovery"]


def test_evals_list_is_empty_when_no_reports_are_stored(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        response = client.get("/api/evals")

        assert response.status_code == 200, response.text
        assert response.json() == {"reports": []}


def test_evals_routes_round_trip_what_the_store_saved(tmp_path: Path) -> None:
    evals_dir = tmp_path / "evals"
    store = EvalReportStore(evals_dir, clock=FixedClock(NOW))
    report = _eval_report()
    store.save(report)

    with _client(tmp_path, _plain_factory(), evals_dir=evals_dir) as client:
        listing = client.get("/api/evals")
        assert listing.status_code == 200, listing.text
        expected_summaries = json.loads(json.dumps([asdict(s) for s in store.list()]))
        assert listing.json() == {"reports": expected_summaries}
        summary = listing.json()["reports"][0]
        assert summary["report_id"] == report.report_id
        assert summary["config_ids"] == ["baseline", "tight-budget"]
        assert summary["task_ids"] == ["bench-provider-outage", "bench-transient-api"]

        detail = client.get(f"/api/evals/{report.report_id}")
        assert detail.status_code == 200, detail.text
        # The wire projection is byte-identical to the operator-owned artifact
        # the store wrote (floats stay finite, ids and rates round-trip).
        assert detail.json() == report_to_dict(report)


def test_eval_detail_unknown_report_id_is_404(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        response = client.get("/api/evals/eval-no-such-report")

        assert response.status_code == 404, response.text
        assert "unknown eval report" in response.json()["detail"]
        # The 404 detail never discloses the server's directory layout.
        assert str(tmp_path) not in response.json()["detail"]


def test_eval_detail_unsafe_report_id_is_404_not_500(tmp_path: Path) -> None:
    # M9 W4: ".." (URL-encoded) is a client addressing error, not store
    # corruption — unknown-resource 404 per the house taxonomy, and the
    # detail must not leak the absolute store path.
    evals_dir = tmp_path / "evals"
    with _client(tmp_path, _plain_factory(), evals_dir=evals_dir) as client:
        response = client.get("/api/evals/%2E%2E")

        assert response.status_code == 404, response.text
        detail = response.json()["detail"]
        assert "not safe for the report store" in detail
        assert str(evals_dir) not in detail


def test_evals_routes_fail_closed_500_on_a_tampered_report(tmp_path: Path) -> None:
    # A hand-edited rate that disagrees with its counts fails domain
    # revalidation: server-side corruption in the 500 {"detail"} family —
    # never a crash, never a 422 blaming a parameterless GET.
    evals_dir = tmp_path / "evals"
    store = EvalReportStore(evals_dir, clock=FixedClock(NOW))
    report = _eval_report()
    path = store.save(report)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["report"]["config_reports"][0]["success_rate"] = 0.75
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    with _client(tmp_path, _plain_factory(), evals_dir=evals_dir) as client:
        detail = client.get(f"/api/evals/{report.report_id}")
        assert detail.status_code == 500, detail.text
        assert "revalidation" in detail.json()["detail"]

        listing = client.get("/api/evals")
        assert listing.status_code == 500, listing.text
        assert "detail" in listing.json()


def test_evals_routes_fail_closed_500_on_an_unreadable_report(tmp_path: Path) -> None:
    # M9 (B1/B2): an unreadable artifact is a 500 {"detail"} naming the file
    # only — never an escaping OSError, never a path leak.
    evals_dir = tmp_path / "evals"
    store = EvalReportStore(evals_dir, clock=FixedClock(NOW))
    report = _eval_report()
    path = store.save(report)
    path.chmod(0o000)
    try:
        with _client(tmp_path, _plain_factory(), evals_dir=evals_dir) as client:
            detail = client.get(f"/api/evals/{report.report_id}")
            assert detail.status_code == 500, detail.text
            message = detail.json()["detail"]
            assert "unreadable" in message
            assert str(evals_dir) not in message
    finally:
        path.chmod(0o644)


def test_evals_dir_defaults_under_the_server_data_dir(tmp_path: Path) -> None:
    # evals_dir unset resolves to data_dir/"evals" so the routes work out of
    # the box, exactly like the other server-owned paths.
    data_dir = tmp_path / "data"
    store = EvalReportStore(data_dir / "evals", clock=FixedClock(NOW))
    report = _eval_report()
    store.save(report)

    with _client(tmp_path, _plain_factory(), data_dir=data_dir) as client:
        listing = client.get("/api/evals")
        assert listing.status_code == 200, listing.text
        assert [entry["report_id"] for entry in listing.json()["reports"]] == [report.report_id]

        detail = client.get(f"/api/evals/{report.report_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json() == report_to_dict(report)


def test_eval_routes_are_read_only(tmp_path: Path) -> None:
    # Reports are operator-owned artifacts: the server only reads them.
    with _client(tmp_path, _plain_factory()) as client:
        assert client.post("/api/evals").status_code == 405
        assert client.delete("/api/evals/eval-report-1").status_code == 405
        assert client.post("/api/benchmark/suite").status_code == 405


# --- Candidate policy registry exposure (PACS-017 M6) --------------------------


def _register_policy(
    registry_dir: Path, policy_id: str = "candidate-x", version: int = 1
) -> PolicyRecord:
    store = PolicyRegistryStore(registry_dir, clock=FixedClock(NOW))
    return store.register(
        ExecutionPolicy(
            policy_id=policy_id,
            version=version,
            context_allocation=ContextAllocationBounds(floor_tokens=1024, ceiling_tokens=4096),
        ),
        evidence_basis=f"registered {policy_id} v{version}",
    )


def test_policies_list_is_empty_out_of_the_box(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        response = client.get("/api/policies")
        assert response.status_code == 200, response.text
        assert response.json() == {"policies": []}


def test_policies_list_and_detail_use_the_artifact_projection(tmp_path: Path) -> None:
    policies_dir = tmp_path / "policies"
    record = _register_policy(policies_dir)
    with _client(tmp_path, _plain_factory(), policies_dir=policies_dir) as client:
        listing = client.get("/api/policies")
        assert listing.status_code == 200, listing.text
        assert listing.json() == {"policies": [policy_record_to_dict(record)]}

        detail = client.get("/api/policies/candidate-x")
        assert detail.status_code == 200, detail.text
        assert detail.json() == policy_record_to_dict(record)


def test_policy_detail_unknown_id_is_404_without_a_path_leak(tmp_path: Path) -> None:
    policies_dir = tmp_path / "policies"
    with _client(tmp_path, _plain_factory(), policies_dir=policies_dir) as client:
        response = client.get("/api/policies/no-such-policy")

        assert response.status_code == 404, response.text
        assert "unknown registered policy" in response.json()["detail"]
        assert str(policies_dir) not in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"evidence_basis": "eval-report-7"},
        {"evidence_basis": "eval-report-7", "confirm": "true"},
        {"evidence_basis": "eval-report-7", "confirm": 1},
        {"evidence_basis": "eval-report-7", "confirm": True, "unexpected": 1},
    ],
    ids=["empty", "no-confirm", "string-confirm", "int-confirm", "extra-key"],
)
def test_policy_promote_rejects_unconfirmed_or_malformed_bodies(
    tmp_path: Path, body: dict[str, object]
) -> None:
    # The pydantic 422 family: only a literal JSON true confirms, and the
    # request shape is closed (extra="forbid").
    policies_dir = tmp_path / "policies"
    _register_policy(policies_dir)
    with _client(tmp_path, _plain_factory(), policies_dir=policies_dir) as client:
        response = client.post("/api/policies/candidate-x/promote", json=body)

        assert response.status_code == 422, response.text


def test_policy_promote_without_an_evidence_basis_is_denied(tmp_path: Path) -> None:
    # Rule 16: a blank basis is rejected by the domain as a 422 even when the
    # operator confirmed and the record is otherwise promotable.
    policies_dir = tmp_path / "policies"
    _register_policy(policies_dir)
    PolicyRegistryStore(policies_dir).transition(
        "candidate-x", 1, PolicyLifecycle.BENCHMARKED, evidence_basis="eval-report-6"
    )
    with _client(tmp_path, _plain_factory(), policies_dir=policies_dir) as client:
        response = client.post(
            "/api/policies/candidate-x/promote",
            json={"evidence_basis": "   ", "confirm": True},
        )

        assert response.status_code == 422, response.text
        assert "evidence_basis" in response.json()["detail"]


def test_policy_promote_a_fresh_candidate_is_denied(tmp_path: Path) -> None:
    # Promotion always passes through an evidence-gathering state first:
    # CANDIDATE -> PROMOTED is not in the domain transition table, so even a
    # confirmed, evidenced request is denied (domain ValueError -> 422).
    policies_dir = tmp_path / "policies"
    _register_policy(policies_dir)
    with _client(tmp_path, _plain_factory(), policies_dir=policies_dir) as client:
        response = client.post(
            "/api/policies/candidate-x/promote",
            json={"evidence_basis": "eval-report-7", "confirm": True},
        )

        assert response.status_code == 422, response.text
        assert "is not legal" in response.json()["detail"]
        # The denied request left the record untouched.
        assert client.get("/api/policies/candidate-x").json()["lifecycle"] == "candidate"


def test_policy_promote_a_benchmarked_candidate_succeeds(tmp_path: Path) -> None:
    policies_dir = tmp_path / "policies"
    _register_policy(policies_dir)
    PolicyRegistryStore(policies_dir).transition(
        "candidate-x", 1, PolicyLifecycle.BENCHMARKED, evidence_basis="eval-report-6"
    )
    with _client(tmp_path, _plain_factory(), policies_dir=policies_dir) as client:
        response = client.post(
            "/api/policies/candidate-x/promote",
            json={"evidence_basis": "eval-report-7", "confirm": True, "note": "clean"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["lifecycle"] == "promoted"
        assert body["evidence_basis"] == "eval-report-7"
        assert body["note"] == "clean"
        # The promotion persisted to the operator-owned artifact.
        reloaded = PolicyRegistryStore(policies_dir).get("candidate-x")
        assert reloaded.lifecycle is PolicyLifecycle.PROMOTED


def test_policy_promote_unknown_id_is_404(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        response = client.post(
            "/api/policies/no-such/promote",
            json={"evidence_basis": "eval-report-7", "confirm": True},
        )

        assert response.status_code == 404, response.text
        assert "unknown registered policy" in response.json()["detail"]
        assert str(tmp_path) not in response.json()["detail"]


def test_policy_promote_selects_the_latest_version_by_default(tmp_path: Path) -> None:
    policies_dir = tmp_path / "policies"
    _register_policy(policies_dir, version=1)
    _register_policy(policies_dir, version=2)
    store = PolicyRegistryStore(policies_dir)
    store.transition("candidate-x", 2, PolicyLifecycle.SHADOWED, evidence_basis="run-1")
    with _client(tmp_path, _plain_factory(), policies_dir=policies_dir) as client:
        response = client.post(
            "/api/policies/candidate-x/promote",
            json={"evidence_basis": "eval-report-7", "confirm": True},
        )

        assert response.status_code == 200, response.text
        assert response.json()["policy"]["version"] == 2


def test_policies_routes_fail_closed_500_on_a_tampered_registry(tmp_path: Path) -> None:
    # A hand-edited lifecycle outside the closed vocabulary fails domain
    # revalidation: server-side corruption in the 500 {"detail"} family.
    policies_dir = tmp_path / "policies"
    _register_policy(policies_dir)
    path = policies_dir / "candidate-x--v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["record"]["lifecycle"] = "enshrined"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with _client(tmp_path, _plain_factory(), policies_dir=policies_dir) as client:
        listing = client.get("/api/policies")
        assert listing.status_code == 500, listing.text
        assert "revalidation" in listing.json()["detail"]


def test_policies_routes_fail_closed_500_on_an_unreadable_artifact(tmp_path: Path) -> None:
    # M9 (B1/B2): an unreadable artifact is a 500 {"detail"} naming the file
    # only — never an escaping OSError, never a path leak.
    policies_dir = tmp_path / "policies"
    _register_policy(policies_dir)
    path = policies_dir / "candidate-x--v1.json"
    path.chmod(0o000)
    try:
        with _client(tmp_path, _plain_factory(), policies_dir=policies_dir) as client:
            listing = client.get("/api/policies")
            assert listing.status_code == 500, listing.text
            detail = listing.json()["detail"]
            assert "unreadable" in detail
            assert str(policies_dir) not in detail
    finally:
        path.chmod(0o644)


def test_policies_dir_defaults_under_the_server_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    record = _register_policy(data_dir / "policies")

    with _client(tmp_path, _plain_factory(), data_dir=data_dir) as client:
        detail = client.get("/api/policies/candidate-x")
        assert detail.status_code == 200, detail.text
        assert detail.json() == policy_record_to_dict(record)


def test_policy_registry_has_no_other_write_surface(tmp_path: Path) -> None:
    # The confirm-gated promote route is the ONLY write: no register, no
    # delete — a candidate can never self-promote because no runtime or
    # generic-REST path can create or mutate records.
    with _client(tmp_path, _plain_factory()) as client:
        assert client.post("/api/policies").status_code == 405
        assert client.delete("/api/policies/candidate-x").status_code == 405
        assert client.put("/api/policies/candidate-x").status_code == 405


# --- Filesystem browse + harness detect (read-only) ----------------------------


def _which(mapping: dict[str, str]) -> Callable[[str], str | None]:
    """A typed ``shutil.which`` stand-in for harness-detection tests."""

    def resolve(name: str) -> str | None:
        return mapping.get(name)

    return resolve


def test_fs_browse_lists_subdirectories_with_worktree_flags(tmp_path: Path) -> None:
    root = tmp_path / "browse-root"
    repo = _repo(root / "repo")
    (root / "plain").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "a-file.txt").touch()

    with _client(tmp_path, _plain_factory()) as client:
        response = client.get("/api/fs/browse", params={"path": str(root)})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["path"] == str(root)
        assert body["parent"] == str(root.parent)
        assert body["is_git_worktree"] is False
        assert body["truncated"] is False
        assert body["notes"] == []
        entries = {entry["name"]: entry for entry in body["entries"]}
        # Directories only — the plain file never appears in the listing.
        assert set(entries) == {repo.name, "plain", ".hidden"}
        assert entries[repo.name]["is_git_worktree"] is True
        assert entries[repo.name]["is_hidden"] is False
        assert entries["plain"]["is_git_worktree"] is False
        assert entries[".hidden"]["is_hidden"] is True
        # Quick-jump shortcuts: home and filesystem root are always offered.
        assert str(Path.home()) in body["shortcuts"]
        assert "/" in body["shortcuts"]


def test_fs_browse_defaults_to_home_and_the_root_is_parentless(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        defaulted = client.get("/api/fs/browse")
        assert defaulted.status_code == 200, defaulted.text
        assert defaulted.json()["path"] == str(Path.home())

        root = client.get("/api/fs/browse", params={"path": "/"})
        assert root.status_code == 200, root.text
        assert root.json()["path"] == "/"
        assert root.json()["parent"] is None


def test_fs_browse_denies_relative_missing_and_non_directory_paths(tmp_path: Path) -> None:
    a_file = tmp_path / "a-file.txt"
    a_file.touch()

    with _client(tmp_path, _plain_factory()) as client:
        relative = client.get("/api/fs/browse", params={"path": "some/relative/dir"})
        assert relative.status_code == 422, relative.text
        assert "absolute" in relative.json()["detail"]

        missing = client.get("/api/fs/browse", params={"path": str(tmp_path / "nope")})
        assert missing.status_code == 404, missing.text
        assert "no such directory" in missing.json()["detail"]

        not_a_dir = client.get("/api/fs/browse", params={"path": str(a_file)})
        assert not_a_dir.status_code == 404, not_a_dir.text
        assert "no such directory" in not_a_dir.json()["detail"]


def test_fs_detect_suggests_pytest_for_python_marker_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "pyproject.toml").touch()
    (repo / "src").mkdir()
    (repo / "tests").mkdir()

    with _client(tmp_path, _plain_factory()) as client:
        response = client.get("/api/fs/detect", params={"path": str(repo)})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_git_worktree"] is True
        assert body["checks"] == [
            {
                "name": "tests",
                "kind": "TEST",
                "argv": ["{python}", "-m", "pytest", "-q"],
                "timeout_seconds": 120,
            }
        ]
        assert body["required"] == ["tests"]
        assert body["allowed_prefixes"] == ["src", "tests"]
        # .venv/bin/python exists (via _repo), so there is nothing to warn about.
        assert body["notes"] == []


def test_fs_detect_notes_a_missing_local_python_for_the_token(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "pyproject.toml").touch()

    with _client(tmp_path, _plain_factory()) as client:
        body = client.get("/api/fs/detect", params={"path": str(repo)}).json()

        assert body["checks"][0]["argv"][0] == "{python}"
        assert any(".venv/bin/python" in note for note in body["notes"])


def test_fs_detect_npm_suggestion_uses_an_absolute_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
    monkeypatch.setattr(fsbrowse.shutil, "which", _which({"npm": "/usr/local/bin/npm"}))

    with _client(tmp_path, _plain_factory()) as client:
        body = client.get("/api/fs/detect", params={"path": str(repo)}).json()

        assert body["checks"] == [
            {
                "name": "tests",
                "kind": "TEST",
                "argv": ["/usr/local/bin/npm", "test"],
                "timeout_seconds": 120,
            }
        ]


def test_fs_detect_npm_without_npm_on_path_is_a_note_not_a_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
    monkeypatch.setattr(fsbrowse.shutil, "which", _which({}))

    with _client(tmp_path, _plain_factory()) as client:
        body = client.get("/api/fs/detect", params={"path": str(repo)}).json()

        assert body["checks"] == []
        assert any("npm is not on PATH" in note for note in body["notes"])


def test_fs_detect_makefile_test_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "Makefile").write_text("test:\n\tpytest -q\n")
    monkeypatch.setattr(fsbrowse.shutil, "which", _which({"make": "/usr/bin/make"}))

    with _client(tmp_path, _plain_factory()) as client:
        body = client.get("/api/fs/detect", params={"path": str(repo)}).json()

        assert body["checks"][0]["argv"] == ["/usr/bin/make", "test"]


def test_fs_detect_an_unrecognized_repo_is_honest_about_absence(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")  # git worktree with no marker files at all

    with _client(tmp_path, _plain_factory()) as client:
        body = client.get("/api/fs/detect", params={"path": str(repo)}).json()

        assert body["checks"] == []
        assert body["required"] == []
        assert "no test harness detected" in body["notes"]
        # No conventional source dirs either — the form field stays operator-owned.
        assert body["allowed_prefixes"] == []
        assert any("allowed_prefixes" in note for note in body["notes"])


def test_fs_detect_flags_a_non_git_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pyproject.toml").touch()

    with _client(tmp_path, _plain_factory()) as client:
        body = client.get("/api/fs/detect", params={"path": str(plain)}).json()

        assert body["is_git_worktree"] is False
        assert any("not a git worktree" in note for note in body["notes"])


def test_fs_detect_denies_relative_and_missing_paths(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        relative = client.get("/api/fs/detect", params={"path": "some/relative/dir"})
        assert relative.status_code == 422, relative.text

        missing = client.get("/api/fs/detect", params={"path": str(tmp_path / "nope")})
        assert missing.status_code == 404, missing.text
        assert "no such directory" in missing.json()["detail"]

        # The query parameter is required for detect.
        assert client.get("/api/fs/detect").status_code == 422


def test_fs_routes_are_read_only(tmp_path: Path) -> None:
    with _client(tmp_path, _plain_factory()) as client:
        assert client.post("/api/fs/browse").status_code == 405
        assert client.post("/api/fs/detect").status_code == 405
        assert client.delete("/api/fs/browse").status_code == 405
        assert client.put("/api/fs/detect").status_code == 405
