"""Session manager units: registry, wiring validation, driving invariants.

Covers the pieces of ``loopforge.entrypoints.sessions`` that are easier to
pin directly than over HTTP: the JSON registry round-trip, inline wiring
fail-closed validation, driver-thread denial rules (rollback while driving,
double start, terminal start), bounded pause while a step is in flight,
driver exception recording, and shutdown closing bundles.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.fanout_store import FanOutEventStore
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
)
from loopforge.application.runtime import Runtime, UnknownRunError
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext
from loopforge.domain.events import Event, RunStarted, RunStopped
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.security import SandboxCapabilities
from loopforge.domain.state import InvalidTransitionError, RunRecord
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
    RunStatus,
    StopReason,
    WorkspaceId,
)
from loopforge.domain.workspace import WorkspaceStatus
from loopforge.entrypoints import sessions as sessions_module
from loopforge.entrypoints.profile import ProfileError, load_profile
from loopforge.entrypoints.repair import RepairRuntimeBundle
from loopforge.entrypoints.sessions import (
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
    SessionWiring,
    UnmanagedRunError,
    build_production_bundle_factory,
    wiring_from_inline,
    wiring_from_profile_path,
)
from loopforge.ports.model import ModelTurn
from loopforge.ports.sandbox import SandboxCommandResult
from loopforge.ports.state_store import StateStorePort
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
    """SandboxPort-conformant fake recording lifecycle."""

    def __init__(self) -> None:
        self.destroyed = False

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

    def destroy(self) -> None:
        self.destroyed = True


class BlockingTools:
    """Tool executor whose execute() blocks until released (deterministic driving tests)."""

    def __init__(self, metadata: list[ToolMetadata]) -> None:
        self._metadata = {item.name: item for item in metadata}
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


@dataclass(slots=True)
class FakeBundleFactory:
    """BundleFactory fake wiring scripted-model runtimes over the app's store."""

    actions: list[ActionProposal] = field(default_factory=list[ActionProposal])
    results: list[ToolResult] = field(default_factory=list[ToolResult])
    tool_metadata: list[ToolMetadata] = field(default_factory=list[ToolMetadata])
    tools_override: BlockingTools | None = None
    bundles: list[RepairRuntimeBundle] = field(default_factory=list[RepairRuntimeBundle])
    workspaces: list[FakeWorkspace] = field(default_factory=list[FakeWorkspace])
    sandboxes: list[FakeSandbox] = field(default_factory=list[FakeSandbox])

    def build(self, wiring: SessionWiring, store: StateStorePort) -> RepairRuntimeBundle:
        del wiring  # the fake does not reconstruct profiles
        workspace = FakeWorkspace()
        sandbox = FakeSandbox()
        tools = (
            self.tools_override
            if self.tools_override is not None
            else ScriptedTools(list(self.results), metadata=list(self.tool_metadata))
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
        bundle = RepairRuntimeBundle(runtime=runtime, workspace=workspace, sandbox=sandbox)
        self.bundles.append(bundle)
        self.workspaces.append(workspace)
        self.sandboxes.append(sandbox)
        return bundle


def _plain_factory() -> FakeBundleFactory:
    return FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        results=[ToolResult(ok=True, observation="all tests pass")],
        tool_metadata=[_metadata("probe", ApprovalClass.NONE)],
    )


def _wiring() -> SessionWiring:
    return SessionWiring(
        objective="fix the checks",
        repository="/nonexistent-repo",
        profile_source={"kind": "path", "path": "/nonexistent-profile.toml"},
        max_cost_usd=5.0,
        max_iterations=30,
        max_total_tokens=None,
        max_elapsed_seconds=None,
        model_provider="scripted",
        model_name="scripted",
        container_image=None,
        created_at=NOW.isoformat(),
    )


def _manager(
    tmp_path: Path,
    factory: FakeBundleFactory,
    *,
    store: FanOutEventStore | None = None,
    registry_path: Path | None = None,
    join_timeout: float = 5.0,
) -> SessionManager:
    return SessionManager(
        store or FanOutEventStore(InMemoryEventStore()),
        SessionRegistry(registry_path or tmp_path / "sessions.json"),
        factory,
        join_timeout_seconds=join_timeout,
    )


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    msg = "condition not met within timeout"
    raise AssertionError(msg)


# --- Registry ---------------------------------------------------------------


def test_registry_round_trip_is_atomic_and_typed(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    registry = SessionRegistry(path)
    wiring = _wiring()

    registry.put("run_1", wiring)
    assert path.is_file()
    # No tmp files linger after an atomic write.
    assert list(tmp_path.glob("*.tmp")) == []

    reloaded = SessionRegistry(path)
    assert reloaded.get("run_1") == wiring
    assert reloaded.get("run_missing") is None
    assert reloaded.as_dict() == {"run_1": wiring}

    other = _wiring()
    registry.put("run_2", other)
    assert set(SessionRegistry(path).as_dict()) == {"run_1", "run_2"}


def test_registry_rejects_a_corrupted_file(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")

    with pytest.raises(SessionRegistryError, match="not a JSON object"):
        SessionRegistry(path).as_dict()

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SessionRegistryError, match="not valid JSON"):
        SessionRegistry(path).as_dict()


def test_registry_rejects_malformed_wiring_entries(tmp_path: Path) -> None:
    valid: dict[str, Any] = asdict(_wiring())
    cases: dict[str, object] = {
        "entry-not-object": ["nope"],
        "bad-profile-source": {**valid, "profile_source": {"kind": 1}},
        "bad-iterations": {**valid, "max_iterations": "30"},
        "missing-cost": {**valid, "max_cost_usd": None},
        "bad-cost-type": {**valid, "max_cost_usd": "five"},
        "bad-tokens": {**valid, "max_total_tokens": "many"},
        "bad-objective": {**valid, "objective": 7},
        "bad-container-image": {**valid, "container_image": 3},
        "bad-elapsed": {**valid, "max_elapsed_seconds": "soon"},
        "bad-created-at": {**valid, "created_at": 0},
    }
    for label, entry in cases.items():
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps({"run_1": entry}), encoding="utf-8")
        with pytest.raises(SessionRegistryError, match="session registry entry drifted"):
            SessionRegistry(path).as_dict()


# --- Inline/profile wiring validation (fail closed) --------------------------


def _repo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").touch()
    return root


def _inline_fields(repo: Path) -> InlineProfileFields:
    return InlineProfileFields(
        repository=str(repo),
        objective="Fix the failing checks.",
        checks=(
            InlineCheckFields(
                name="unit_tests",
                kind="TEST",
                argv=("{python}", "-m", "pytest", "-q"),
                timeout_seconds=300.0,
            ),
        ),
        acceptance=InlineAcceptanceFields(
            required=("unit_tests",), allowed_prefixes=("src", "tests")
        ),
        model=InlineModelFields(provider="scripted", tier="economy"),
        budget=InlineBudgetFields(max_cost_usd=5.0, max_iterations=30),
    )


def test_inline_wiring_renders_validates_and_reconstructs_identically(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    fields = _inline_fields(repo)

    wiring = wiring_from_inline(fields, profiles_dir=tmp_path / "profiles")

    assert wiring.profile_source["kind"] == "inline"
    assert wiring.objective == "Fix the failing checks."
    assert wiring.repository == str(repo)
    assert wiring.max_cost_usd == 5.0
    assert wiring.max_iterations == 30
    assert wiring.model_provider == "scripted"
    # The rendered TOML is persisted under the profiles dir and reparses to
    # the identical profile — restart reconstruction is byte-identical.
    persisted = list((tmp_path / "profiles").glob("inline-*.toml"))
    assert len(persisted) == 1
    assert persisted[0].read_text(encoding="utf-8") == wiring.profile_source["toml"]
    profile = load_profile(persisted[0])
    assert profile.task.objective == fields.objective
    assert [command.name for command in profile.task.commands] == ["unit_tests"]
    assert profile.budget.max_iterations == 30


def test_inline_wiring_renders_optional_tables(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    fields = InlineProfileFields(
        repository=str(repo),
        objective="Container objective.",
        task_id="inline-container",
        checks=(
            InlineCheckFields(
                name="tests",
                kind="TEST",
                argv=("/usr/local/bin/python", "-m", "pytest"),
                timeout_seconds=120.0,
                cpu_seconds=100,
            ),
        ),
        acceptance=InlineAcceptanceFields(
            required=("tests",),
            allowed_prefixes=("src",),
            require_change=True,
            max_changed_files=10,
        ),
        sandbox=InlineSandboxFields(
            container_image="loopforge:test",
            environment={"CI": "true"},
            max_memory_bytes=1024 * 1024 * 1024,
        ),
        model=InlineModelFields(provider="ollama", name="devstral", tier="standard"),
        budget=InlineBudgetFields(
            max_cost_usd=2.5,
            max_iterations=10,
            max_total_tokens=100_000,
            max_elapsed_seconds=3600.0,
        ),
    )

    wiring = wiring_from_inline(fields, profiles_dir=tmp_path / "profiles")

    assert wiring.container_image == "loopforge:test"
    assert wiring.model_name == "devstral"
    assert wiring.max_total_tokens == 100_000
    assert wiring.max_elapsed_seconds == 3600.0


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param("bad-budget", id="bad-budget"),
        pytest.param("unknown-check-kind", id="unknown-check-kind"),
        pytest.param("missing-repository", id="missing-repository"),
        pytest.param("unknown-provider", id="unknown-provider"),
    ],
)
def test_inline_wiring_fails_closed_on_invalid_profiles(tmp_path: Path, mutate: str) -> None:
    repo = _repo(tmp_path / "repo")
    fields = _inline_fields(repo)
    if mutate == "bad-budget":
        fields = replace(fields, budget=InlineBudgetFields(max_cost_usd=-1.0, max_iterations=0))
    elif mutate == "unknown-check-kind":
        check = fields.checks[0]
        fields = replace(
            fields,
            checks=(replace(check, kind="BOGUS"),),
        )
    elif mutate == "missing-repository":
        fields = replace(fields, repository=str(tmp_path / "missing"))
    else:
        fields = replace(fields, model=InlineModelFields(provider="bogus", tier="economy"))

    with pytest.raises(ProfileError):
        wiring_from_inline(fields, profiles_dir=tmp_path / "profiles")


def test_wiring_from_profile_path_validates_eagerly(tmp_path: Path) -> None:
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

    wiring = wiring_from_profile_path(profile_path)

    assert wiring.profile_source == {"kind": "path", "path": str(profile_path)}
    assert wiring.objective == "Fix it via path."

    with pytest.raises(ProfileError, match="not found"):
        wiring_from_profile_path(tmp_path / "missing.toml")


# --- Driving invariants -------------------------------------------------------


def test_rollback_is_denied_while_driving_and_allowed_once_quiescent(
    tmp_path: Path,
) -> None:
    tools = BlockingTools([_metadata("probe", ApprovalClass.NONE)])
    factory = FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        tool_metadata=[_metadata("probe", ApprovalClass.NONE)],
        tools_override=tools,
    )
    manager = _manager(tmp_path, factory)
    run_id = manager.create_session(_wiring())

    manager.start_driving(run_id)
    assert tools.entered.wait(timeout=5)

    with pytest.raises(SessionStateError, match="driving"):
        manager.rollback(run_id)
    with pytest.raises(SessionStateError, match="already driving"):
        manager.start_driving(run_id)

    tools.release.set()
    _wait_for(lambda: manager.state(run_id).status is RunStatus.SUCCEEDED)

    manager.rollback(run_id)
    assert factory.workspaces[-1].reset_calls == 1
    manager.rollback(run_id, ("src/a.py", "src/b.py"))
    assert factory.workspaces[-1].checkout_calls == [("src/a.py", "src/b.py")]


def test_start_driving_denied_on_terminal_and_unknown_runs(tmp_path: Path) -> None:
    factory = _plain_factory()
    manager = _manager(tmp_path, factory)
    run_id = manager.create_session(_wiring())

    manager.start_driving(run_id)
    _wait_for(lambda: manager.state(run_id).status is RunStatus.SUCCEEDED)

    with pytest.raises(SessionStateError, match="terminal"):
        manager.start_driving(run_id)
    with pytest.raises(UnknownRunError, match="no persisted run"):
        manager.start_driving(RunId("run_missing"))
    # Resuming a terminal run is a documented no-op.
    assert manager.resume(run_id).status is RunStatus.SUCCEEDED


# --- Follow-up (PACS-014b) -----------------------------------------------------


def test_follow_up_clones_wiring_with_a_report_seeded_objective(tmp_path: Path) -> None:
    factory = _plain_factory()
    manager = _manager(tmp_path, factory)
    run_id = manager.create_session(_wiring())
    manager.start_driving(run_id)
    _wait_for(lambda: manager.state(run_id).status is RunStatus.SUCCEEDED)

    successor_id = manager.follow_up(run_id)

    assert successor_id != run_id
    assert manager.state(successor_id).status is RunStatus.READY
    source_wiring = manager.wiring(run_id)
    successor_wiring = manager.wiring(successor_id)
    assert source_wiring is not None
    assert successor_wiring is not None
    assert successor_wiring.repository == source_wiring.repository
    assert successor_wiring.profile_source == source_wiring.profile_source
    assert successor_wiring.max_iterations == source_wiring.max_iterations
    assert successor_wiring.created_at != source_wiring.created_at
    objective = successor_wiring.objective
    assert objective.startswith("fix the checks")
    assert f"Follow-up report from run {run_id}" in objective
    assert "success_verified" in objective
    assert "build on its verified state" in objective
    assert "probe x1" in objective
    # The new session is quiescent: the operator reviews and starts it.
    listing = {entry["run_id"]: entry for entry in manager.list_sessions()}
    assert listing[successor_id]["driving"] is False


def test_follow_up_records_the_parent_run_id_durably(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _plain_factory())
    run_id = manager.create_session(_wiring())
    manager.start_driving(run_id)
    _wait_for(lambda: manager.state(run_id).status is RunStatus.SUCCEEDED)

    successor_id = manager.follow_up(run_id)

    # The wiring mirrors the structured link...
    successor_wiring = manager.wiring(successor_id)
    assert successor_wiring is not None
    assert successor_wiring.parent_run_id == run_id
    # ...and the authoritative stream carries it on RunStarted, projected by
    # the reducer — lineage never depends on parsing the report text.
    started = manager.events(successor_id)[0]
    assert isinstance(started, RunStarted)
    assert started.parent_run_id == RunId(run_id)
    assert manager.state(successor_id).parent_run_id == RunId(run_id)


def test_lineage_resolves_a_two_hop_chain_in_order(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _plain_factory())
    run_id = manager.create_session(_wiring())
    manager.start_driving(run_id)
    _wait_for(lambda: manager.state(run_id).status is RunStatus.SUCCEEDED)
    successor_id = manager.follow_up(run_id)
    manager.start_driving(successor_id)
    _wait_for(lambda: manager.state(successor_id).status is RunStatus.SUCCEEDED)

    grandchild_id = manager.follow_up(successor_id)

    lineage = manager.lineage(grandchild_id)
    assert lineage.parent_run_id == successor_id
    assert lineage.ancestors == (successor_id, run_id)
    assert manager.lineage(run_id).parent_run_id is None
    assert manager.lineage(run_id).children == (successor_id,)
    assert manager.lineage(successor_id).children == (grandchild_id,)


def test_lineage_walk_is_bounded_and_cycle_safe(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _plain_factory())
    run_a = manager.create_session(_wiring())
    manager.start_driving(run_a)
    _wait_for(lambda: manager.state(run_a).status is RunStatus.SUCCEEDED)
    run_b = manager.create_session(_wiring())

    # A hand-edited or corrupt registry can link parents in a cycle;
    # resolution must terminate instead of wedging the debugging surface.
    registry = SessionRegistry(tmp_path / "sessions.json")
    wiring_a = registry.get(run_a)
    wiring_b = registry.get(run_b)
    assert wiring_a is not None
    assert wiring_b is not None
    registry.put(run_a, replace(wiring_a, parent_run_id=run_b))
    registry.put(run_b, replace(wiring_b, parent_run_id=run_a))

    lineage = manager.lineage(run_a)
    assert lineage.parent_run_id == run_b
    assert lineage.ancestors == (run_b,)  # the cycle back to run_a is cut


def test_lineage_rejects_an_unknown_run(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _plain_factory())

    with pytest.raises(UnknownRunError, match="no persisted run"):
        manager.lineage("run_missing")


def test_follow_up_is_denied_for_a_non_terminal_run(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _plain_factory())
    run_id = manager.create_session(_wiring())

    with pytest.raises(SessionStateError, match="follow-up requires a terminal"):
        manager.follow_up(run_id)


def test_follow_up_is_denied_for_an_unmanaged_run(tmp_path: Path) -> None:
    store = FanOutEventStore(InMemoryEventStore())
    factory = _plain_factory()
    manager_a = _manager(tmp_path, factory, store=store)
    run_id = manager_a.create_session(_wiring())
    manager_a.start_driving(run_id)
    _wait_for(lambda: manager_a.state(run_id).status is RunStatus.SUCCEEDED)

    manager_b = _manager(
        tmp_path, factory, store=store, registry_path=tmp_path / "other" / "sessions.json"
    )
    with pytest.raises(UnmanagedRunError, match="no wiring to follow up from"):
        manager_b.follow_up(run_id)


def test_pause_is_bounded_while_a_step_is_in_flight(tmp_path: Path) -> None:
    tools = BlockingTools([_metadata("probe", ApprovalClass.NONE)])
    factory = FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        tool_metadata=[_metadata("probe", ApprovalClass.NONE)],
        tools_override=tools,
    )
    manager = _manager(tmp_path, factory, join_timeout=0.2)
    run_id = manager.create_session(_wiring())
    manager.start_driving(run_id)
    assert tools.entered.wait(timeout=5)

    started = time.monotonic()
    manager.pause(run_id)  # the in-flight step finishes first; join is bounded
    assert time.monotonic() - started < 5

    tools.release.set()
    _wait_for(lambda: not bool(manager.list_sessions()[0]["driving"]))
    manager.shutdown()


def test_pause_and_state_on_unknown_runs_fail_closed(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _plain_factory())

    with pytest.raises(UnknownRunError, match="no persisted run"):
        manager.pause(RunId("run_missing"))
    with pytest.raises(UnknownRunError, match="no persisted run"):
        manager.state(RunId("run_missing"))
    with pytest.raises(UnknownRunError, match="no persisted run"):
        manager.events(RunId("run_missing"))


def test_unmanaged_run_is_listed_but_driving_is_denied(tmp_path: Path) -> None:
    store = FanOutEventStore(InMemoryEventStore())
    factory = _plain_factory()
    manager_a = _manager(tmp_path, factory, store=store)
    run_id = manager_a.create_session(_wiring())

    # A second manager over the same store but a different (empty) registry —
    # the run exists in the store but is not managed by this server.
    manager_b = _manager(
        tmp_path, factory, store=store, registry_path=tmp_path / "other" / "sessions.json"
    )
    listing = manager_b.list_sessions()
    assert [(entry["run_id"], entry["managed"]) for entry in listing] == [(run_id, False)]
    assert manager_b.state(run_id).status is RunStatus.READY
    assert manager_b.wiring(run_id) is None

    with pytest.raises(UnmanagedRunError, match="not managed"):
        manager_b.resume(run_id)
    with pytest.raises(UnmanagedRunError, match="not managed"):
        manager_b.rollback(run_id)


def test_driver_exception_is_recorded_never_silent(tmp_path: Path) -> None:
    factory = FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        # The verifier never passes and the scripted model exhausts on the
        # second turn: the escaping error must land on the session.
        results=[ToolResult(ok=True, observation="still failing")],
        tool_metadata=[_metadata("probe", ApprovalClass.NONE)],
    )
    manager = _manager(tmp_path, factory)
    run_id = manager.create_session(_wiring())

    manager.start_driving(run_id)
    _wait_for(lambda: manager.session_error(run_id) is not None)

    assert isinstance(manager.session_error(run_id), RuntimeError)
    assert not bool(manager.list_sessions()[0]["driving"])
    manager.shutdown()


# --- Profile source reconstruction and the production factory -----------------


def test_profile_from_wiring_reconstructs_all_sources(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    inline = wiring_from_inline(_inline_fields(repo), profiles_dir=tmp_path / "profiles")

    profile = sessions_module._profile_from_wiring(inline, tmp_path / "profiles")  # pyright: ignore[reportPrivateUsage]
    assert profile.task.objective == "Fix the failing checks."

    profile_path = tmp_path / "profile.toml"
    profile_path.write_text(inline.profile_source["toml"], encoding="utf-8")
    path_wiring = wiring_from_profile_path(profile_path)
    from_path = sessions_module._profile_from_wiring(path_wiring, tmp_path / "profiles")  # pyright: ignore[reportPrivateUsage]
    assert from_path.task.objective == "Fix the failing checks."

    bogus = replace(inline, profile_source={"kind": "bogus"})
    with pytest.raises(ProfileError, match="unknown profile source kind"):
        sessions_module._profile_from_wiring(bogus, tmp_path / "profiles")  # pyright: ignore[reportPrivateUsage]


_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git executable unavailable; production-factory tests adopt a real checkout",
)


def _git_repo(root: Path) -> Path:
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").touch()
    (root / "module.py").write_text("value = 1\n", encoding="utf-8")
    commands = [
        ["init"],
        ["add", "."],
        [
            "-c",
            "user.name=loopforge-test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
    ]
    for args in commands:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


class _FakeLiveModel:
    """ModelPort-conformant fake recording lifecycle (no network)."""

    def __init__(self) -> None:
        self.closed = False

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            provider="fake",
            model="fake",
            supports_tool_calls=True,
            context_window_tokens=4096,
        )

    def propose_action(self, context: ModelContext) -> ModelTurn:
        del context
        msg = "the fake live model never serves turns in wiring tests"
        raise AssertionError(msg)

    def close(self) -> None:
        self.closed = True


@_REQUIRES_GIT
def test_production_factory_builds_a_scripted_bundle_from_inline_wiring(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    wiring = wiring_from_inline(_inline_fields(repo), profiles_dir=tmp_path / "profiles")
    factory = build_production_bundle_factory(tmp_path / "profiles")
    store = InMemoryEventStore()

    bundle = factory.build(wiring, store)
    try:
        assert bundle.runtime.store is store
        assert bundle.workspace.root == repo.resolve()
        assert bundle.runtime.control.no_progress_limit == 3
    finally:
        bundle.close()


@_REQUIRES_GIT
def test_production_factory_applies_the_profile_no_progress_limit(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    fields = replace(
        _inline_fields(repo),
        budget=InlineBudgetFields(max_cost_usd=5.0, max_iterations=30, no_progress_limit=7),
    )
    wiring = wiring_from_inline(fields, profiles_dir=tmp_path / "profiles")
    factory = build_production_bundle_factory(tmp_path / "profiles")

    bundle = factory.build(wiring, InMemoryEventStore())
    try:
        assert bundle.runtime.control.no_progress_limit == 7
    finally:
        bundle.close()


@_REQUIRES_GIT
def test_production_factory_maps_live_providers_and_closes_on_build_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "repo")
    built: list[tuple[str, str, _FakeLiveModel]] = []

    def _fake_deepseek(task: Any, *, model_name: str) -> _FakeLiveModel:
        del task
        model = _FakeLiveModel()
        built.append(("deepseek", model_name, model))
        return model

    def _fake_ollama(
        task: Any, *, model_name: str, base_url: str, context_window_tokens: int
    ) -> _FakeLiveModel:
        del task, base_url, context_window_tokens
        model = _FakeLiveModel()
        built.append(("ollama", model_name, model))
        return model

    monkeypatch.setattr(sessions_module, "build_deepseek_model", _fake_deepseek)
    monkeypatch.setattr(sessions_module, "build_ollama_model", _fake_ollama)

    factory = build_production_bundle_factory(tmp_path / "profiles")
    for provider in ("deepseek", "ollama"):
        fields = replace(
            _inline_fields(repo),
            model=InlineModelFields(provider=provider, name="m", tier="standard"),
        )
        wiring = wiring_from_inline(fields, profiles_dir=tmp_path / "profiles")
        bundle = factory.build(wiring, InMemoryEventStore())
        bundle.close()
    assert [(kind, name) for kind, name, _ in built] == [
        ("deepseek", "m"),
        ("ollama", "m"),
    ]
    assert all(model.closed for _, _, model in built)

    # Failure path: when bundle construction fails after the model exists, the
    # factory closes the model rather than leaking its client.
    def _raising_build(*args: Any, **kwargs: Any) -> RepairRuntimeBundle:
        del args, kwargs
        msg = "boom"
        raise ValueError(msg)

    monkeypatch.setattr(sessions_module, "build_adopted_repair_runtime", _raising_build)
    fields = replace(
        _inline_fields(repo),
        model=InlineModelFields(provider="deepseek", name="m", tier="standard"),
    )
    wiring = wiring_from_inline(fields, profiles_dir=tmp_path / "profiles")
    with pytest.raises(ValueError, match="boom"):
        factory.build(wiring, InMemoryEventStore())
    assert built[-1][2].closed is True


def test_shutdown_pauses_drivers_and_closes_bundles(tmp_path: Path) -> None:
    tools = BlockingTools([_metadata("probe", ApprovalClass.NONE)])
    factory = FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        tool_metadata=[_metadata("probe", ApprovalClass.NONE)],
        tools_override=tools,
    )
    manager = _manager(tmp_path, factory, join_timeout=0.2)
    run_id = manager.create_session(_wiring())
    manager.start_driving(run_id)
    assert tools.entered.wait(timeout=5)

    manager.shutdown()  # bounded join: the in-flight step is abandoned, not hung

    assert factory.sandboxes[-1].destroyed is True
    assert manager.list_sessions()[0]["managed"] is True
    tools.release.set()  # let the abandoned driver thread finish


@_REQUIRES_GIT
def test_production_factory_gates_operator_selected_tools(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    fields = replace(
        _inline_fields(repo),
        approval_required_for=("write_file", "edit_file"),
    )
    wiring = wiring_from_inline(fields, profiles_dir=tmp_path / "profiles")
    factory = build_production_bundle_factory(tmp_path / "profiles")

    bundle = factory.build(wiring, InMemoryEventStore())
    try:
        assert bundle.runtime.tools.metadata_for("write_file").approval is ApprovalClass.REQUIRED
        assert bundle.runtime.tools.metadata_for("edit_file").approval is ApprovalClass.REQUIRED
        assert bundle.runtime.tools.metadata_for("read_file").approval is ApprovalClass.NONE
    finally:
        bundle.close()

    # Unknown gated names fail closed at wiring time, before any run starts.
    bogus = replace(_inline_fields(repo), approval_required_for=("deploy_prod",))
    bogus_wiring = wiring_from_inline(bogus, profiles_dir=tmp_path / "profiles")
    with pytest.raises(ValueError, match="not registered"):
        factory.build(bogus_wiring, InMemoryEventStore())


# --- Hardening: lifecycle cleanup, repository exclusivity, shutdown locking ----


class _FailingStore:
    """StateStorePort-conformant stub whose append always fails (store down)."""

    def append(self, event: Event, *, expected_version: int) -> int:
        del event, expected_version
        msg = "store down"
        raise RuntimeError(msg)

    def events_for(self, run_id: RunId) -> tuple[Event, ...]:
        del run_id
        return ()

    def current_version(self, run_id: RunId) -> int:
        del run_id
        return 0

    def list_runs(self) -> tuple[RunRecord, ...]:
        return ()


def test_create_session_closes_the_bundle_when_start_fails(tmp_path: Path) -> None:
    """A failed runtime.start must not leak the freshly built bundle."""
    factory = _plain_factory()
    manager = _manager(tmp_path, factory, store=FanOutEventStore(_FailingStore()))

    with pytest.raises(RuntimeError, match="store down"):
        manager.create_session(_wiring())

    assert len(factory.bundles) == 1
    assert factory.sandboxes[0].destroyed is True


def test_create_session_tears_down_when_registration_fails(tmp_path: Path) -> None:
    """A registry failure must never leave a live-but-unmanaged run behind."""

    class _PutFailingRegistry(SessionRegistry):
        """Registry whose put fails (disk full) while reads still work."""

        def put(self, run_id: str, wiring: SessionWiring) -> None:
            del run_id, wiring
            msg = "disk full"
            raise OSError(msg)

    store = FanOutEventStore(InMemoryEventStore())
    factory = _plain_factory()
    manager = SessionManager(
        store,
        _PutFailingRegistry(tmp_path / "sessions.json"),
        factory,
    )

    with pytest.raises(OSError, match="disk full"):
        manager.create_session(_wiring())

    # The bundle is closed, the durable run is cancelled (never left READY),
    # and no live session lingers in memory.
    assert factory.sandboxes[0].destroyed is True
    (run_record,) = store.list_runs()
    assert run_record.status is RunStatus.CANCELLED
    stream = store.events_for(run_record.run_id)
    stopped = [event for event in stream if isinstance(event, RunStopped)]
    assert [event.reason for event in stopped] == [StopReason.CANCELLED]
    # The run is visible but NOT drivable (it was never registered) — and
    # terminal besides.
    (entry,) = manager.list_sessions()
    assert entry["managed"] is False
    assert entry["status"] == "cancelled"


def test_second_session_on_the_same_repository_is_denied(tmp_path: Path) -> None:
    """One active session per adopted checkout (deny + allow pair)."""
    factory = _plain_factory()
    manager = _manager(tmp_path, factory)
    first = manager.create_session(_wiring())

    with pytest.raises(SessionStateError, match="already has an active session"):
        manager.create_session(_wiring())

    # Once the first run is terminal the claim is released.
    manager.stop(first)
    second = manager.create_session(_wiring())

    assert second != first
    manager.shutdown()


def test_repository_claim_survives_a_restart(tmp_path: Path) -> None:
    """The exclusivity claim derives from the durable registry, not memory."""
    store = FanOutEventStore(InMemoryEventStore())
    registry_path = tmp_path / "sessions.json"
    factory = _plain_factory()
    manager_a = _manager(tmp_path, factory, store=store, registry_path=registry_path)
    manager_a.create_session(_wiring())
    manager_a.shutdown()

    # A fresh manager over the same store + registry still sees the live run.
    manager_b = _manager(tmp_path, factory, store=store, registry_path=registry_path)

    with pytest.raises(SessionStateError, match="already has an active session"):
        manager_b.create_session(_wiring())


def test_registry_sweeps_stale_tmp_files_on_init(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    stale = tmp_path / "sessions.json.4242.tmp"
    stale.write_text("garbage from a crashed write", encoding="utf-8")

    SessionRegistry(path)

    assert not stale.exists()


def test_shutdown_does_not_close_the_bundle_under_an_in_flight_step(tmp_path: Path) -> None:
    """bundle.close() serializes behind a live step via the per-run lock."""
    tools = BlockingTools([_metadata("probe", ApprovalClass.NONE)])
    factory = FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        tools_override=tools,
    )
    manager = _manager(tmp_path, factory, join_timeout=0.05)
    run_id = manager.create_session(_wiring())
    manager.start_driving(run_id)
    assert tools.entered.wait(timeout=5)

    shutdown_thread = threading.Thread(target=manager.shutdown)
    shutdown_thread.start()
    # Well past the bounded driver join: the step is still in flight, so the
    # bundle (and its sandbox) must NOT be torn down underneath it.
    time.sleep(0.3)
    assert factory.sandboxes[0].destroyed is False

    tools.release.set()
    shutdown_thread.join(timeout=10)

    assert not shutdown_thread.is_alive()
    assert factory.sandboxes[0].destroyed is True


@dataclass(slots=True)
class _BarrierFactory(FakeBundleFactory):
    """FakeBundleFactory whose build blocks until two builders race inside it."""

    armed: bool = False
    barrier: threading.Barrier = field(default_factory=lambda: threading.Barrier(2), repr=False)

    def build(self, wiring: SessionWiring, store: StateStorePort) -> RepairRuntimeBundle:
        if self.armed:
            self.barrier.wait(timeout=10)
        return super().build(wiring, store)


def test_ensure_session_rebuild_race_keeps_one_session_and_closes_the_loser(
    tmp_path: Path,
) -> None:
    """Two concurrent bundle rebuilds: exactly one survives; the loser is closed."""
    factory = _BarrierFactory(
        actions=[_proposal("a1", "probe")],
        results=[ToolResult(ok=True, observation="all tests pass")],
        tool_metadata=[_metadata("probe", ApprovalClass.NONE)],
    )
    manager = _manager(tmp_path, factory)
    run_id = manager.create_session(_wiring())
    manager.shutdown()  # evict the live session; store + registry persist

    factory.armed = True
    results: list[object] = []
    errors: list[BaseException] = []

    def race() -> None:
        try:
            results.append(
                manager._ensure_session(RunId(run_id))  # pyright: ignore[reportPrivateUsage]
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    assert len(results) == 2
    assert results[0] is results[1]
    # create + two racing rebuilds = three bundles; exactly one racing loser
    # was closed (sandboxes[0] was already closed by the first shutdown).
    assert len(factory.bundles) == 3
    assert [sandbox.destroyed for sandbox in factory.sandboxes[1:]].count(True) == 1
    manager.shutdown()


# --- force-release (PACS-015) -------------------------------------------------


def test_force_release_frees_the_repository_claim_end_to_end(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _plain_factory())
    zombie = manager.create_session(_wiring())
    with pytest.raises(SessionStateError, match="already has an active session"):
        manager.create_session(_wiring())

    manager.force_release(zombie, summary="server died mid-drive")

    assert manager.state(zombie).status is RunStatus.CANCELLED
    successor = manager.create_session(_wiring())
    assert successor != zombie


def test_force_release_denies_a_driving_run(tmp_path: Path) -> None:
    tools = BlockingTools([_metadata("probe", ApprovalClass.NONE)])
    factory = FakeBundleFactory(
        actions=[_proposal("a1", "probe")],
        tool_metadata=[_metadata("probe", ApprovalClass.NONE)],
        tools_override=tools,
    )
    manager = _manager(tmp_path, factory)
    run_id = manager.create_session(_wiring())
    manager.start_driving(run_id)
    assert tools.entered.wait(timeout=5)

    with pytest.raises(SessionStateError, match="driving"):
        manager.force_release(run_id)

    tools.release.set()
    _wait_for(lambda: manager.state(run_id).status is RunStatus.SUCCEEDED)
    manager.shutdown()


def test_force_release_denies_a_terminal_run(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _plain_factory())
    run_id = manager.create_session(_wiring())
    manager.stop(run_id)

    with pytest.raises(SessionStateError, match="already terminal"):
        manager.force_release(run_id)

    # The denial is read-only: no second stop record was appended.
    stops = [e for e in manager.events(run_id) if isinstance(e, RunStopped)]
    assert len(stops) == 1


def test_force_release_denies_an_unknown_run(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _plain_factory())

    with pytest.raises(UnknownRunError, match="no persisted run"):
        manager.force_release(RunId("run_ghost"))


def test_force_release_succeeds_where_stop_fails_on_a_corrupted_stream(tmp_path: Path) -> None:
    store = FanOutEventStore(InMemoryEventStore())
    manager = _manager(tmp_path, _plain_factory(), store=store)
    run_id = manager.create_session(_wiring())
    rid = RunId(run_id)
    # Corrupt the stream: a second RunStarted is never a legal transition, so
    # every replay-based command fails from here on.
    version = store.current_version(rid)
    store.append(
        RunStarted(
            event_id=EventId("evt_corruption"),
            run_id=rid,
            occurred_at=NOW,
            sequence=version + 1,
            objective="forged restart",
        ),
        expected_version=version,
    )

    with pytest.raises(InvalidTransitionError):
        manager.stop(run_id)

    manager.force_release(run_id, summary="corrupted stream release")

    events = store.events_for(rid)
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    stop = events[-1]
    assert isinstance(stop, RunStopped)
    assert stop.reason is StopReason.CANCELLED
    assert stop.summary == "corrupted stream release"


class _FlakyStore(InMemoryEventStore):
    """Simulates a transient store read failure (e.g. sqlite 'database is locked')."""

    def __init__(self) -> None:
        super().__init__()
        self.reads_locked = False

    def events_for(self, run_id: RunId) -> tuple[Event, ...]:
        if self.reads_locked:
            msg = "database is locked"
            raise RuntimeError(msg)
        return super().events_for(run_id)


def test_force_release_propagates_transient_store_errors_without_appending(
    tmp_path: Path,
) -> None:
    # A transient read failure says nothing about the run's terminality: it
    # must NOT be swallowed into the corrupted-stream fallback (which would
    # skip the deny-checks and append anyway). It propagates, and nothing is
    # appended.
    backing = _FlakyStore()
    store = FanOutEventStore(backing)
    manager = _manager(tmp_path, _plain_factory(), store=store)
    run_id = manager.create_session(_wiring())
    rid = RunId(run_id)
    version = store.current_version(rid)
    backing.reads_locked = True

    with pytest.raises(RuntimeError, match="database is locked"):
        manager.force_release(run_id)

    assert store.current_version(rid) == version
