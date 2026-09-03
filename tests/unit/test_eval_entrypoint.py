"""EvalTrialDriver, evidence projection, and presets (PACS-016, M6).

Sandbox-free pins run everywhere: preset resolution (allow + deny), the
check-name projection over durable verification summaries, budget narrowing
(config may tighten, never widen — AGENTS.md rule 12), fail-closed container
resolution (rule 13), loud driver-failure wrapping, and the approval-grant
driving loop over a fully scripted runtime. Evidence-projection pins
materialize the locked fixtures with the real Git adapter (no command
execution); the end-to-end trusted-sandbox trials are gated on the same
RLIMIT_AS/git precedent as the other benchmark tests.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.git_workspace import GitWorkspace, GitWorkspaceManager
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
)
from loopforge.application.eval_runner import EvalConfiguration
from loopforge.application.runtime import Runtime
from loopforge.application.trajectory import count_human_interventions
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import (
    ApprovalGranted,
    RunStopped,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy
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
)
from loopforge.entrypoints.eval import (
    EvalTrialDriver,
    EvalTrialError,
    _check_names_from_summaries,  # pyright: ignore[reportPrivateUsage] - unit pins target the wiring internals
    _evidence_for,  # pyright: ignore[reportPrivateUsage] - unit pins target the wiring internals
    _narrow_budget,  # pyright: ignore[reportPrivateUsage] - unit pins target the wiring internals
    resolve_configurations,
)
from loopforge.ports.model import ModelPort
from loopforge.ports.tools import ToolResult
from loopforge.workloads.benchmarks import (
    AMBIGUOUS_NAIVE_SOLUTION,
    BenchmarkTaskBinding,
    build_benchmark_binding,
)
from loopforge.workloads.repair import OrchestratedRepairTask, RepairTask

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

_TESTS_PREFIX = "tests/"

_BASELINE = EvalConfiguration(config_id="baseline", max_cost_usd=1.0, max_iterations=8)
_TIGHT = EvalConfiguration(
    config_id="tight-budget", max_cost_usd=0.05, max_iterations=4, no_progress_limit=2
)


def _rlimit_as_supported() -> bool:
    probe = "import resource; resource.setrlimit(resource.RLIMIT_AS, (268435456, 268435456))"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git executable unavailable; fixture materialization requires the Git CLI",
)
_REQUIRES_RLIMIT_AS = pytest.mark.skipif(
    not _rlimit_as_supported(),
    reason=(
        "platform rejects setrlimit(RLIMIT_AS); local sandbox launcher cannot apply "
        "resource limits, so command execution fails closed"
    ),
)


def _driver(workspaces_root: Path) -> EvalTrialDriver:
    return EvalTrialDriver(
        workspaces_root=workspaces_root,
        clock_factory=lambda: FixedClock(NOW),
        sleeper_factory=RecordingSleeper,
    )


def _verification(
    event_type: type[VerificationPassed] | type[VerificationFailed],
    summary: str,
    sequence: int,
) -> VerificationPassed | VerificationFailed:
    return event_type(
        event_id=EventId(f"evt-{sequence}"),
        run_id=RunId("run-evidence"),
        occurred_at=NOW,
        sequence=sequence,
        summary=summary,
    )


# --- Presets -----------------------------------------------------------------


def test_baseline_preset_is_pinned() -> None:
    (config,) = resolve_configurations(("baseline",))

    assert config == _BASELINE
    assert config.no_progress_limit == 3
    assert config.router_enabled is False
    assert config.expensive_models == frozenset()


def test_tight_budget_preset_is_pinned() -> None:
    (config,) = resolve_configurations(("tight-budget",))

    assert config == _TIGHT


def test_resolve_preserves_requested_order() -> None:
    configs = resolve_configurations(("tight-budget", "baseline"))

    assert [config.config_id for config in configs] == ["tight-budget", "baseline"]


@pytest.mark.parametrize(
    "names",
    [(), ("bogus",), ("baseline", "nope"), ("baseline", "baseline")],
    ids=["empty", "unknown", "unknown-after-known", "duplicate"],
)
def test_resolve_fails_closed(names: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="preset"):
        resolve_configurations(names)


# --- Budget narrowing (rule 12: config may narrow, never widen) ----------------


def test_narrow_budget_never_widens_the_single_runtime_envelope() -> None:
    wide = EvalConfiguration(config_id="cfg", max_cost_usd=99.0, max_iterations=99)

    assert _narrow_budget(wide, orchestrated=False) == BudgetLimit(
        max_cost_usd=1.0, max_iterations=8
    )


def test_narrow_budget_keeps_tighter_configuration_axes() -> None:
    assert _narrow_budget(_TIGHT, orchestrated=False) == BudgetLimit(
        max_cost_usd=0.05, max_iterations=4
    )


def test_narrow_budget_uses_the_orchestrated_envelope() -> None:
    wide = EvalConfiguration(config_id="cfg", max_cost_usd=99.0, max_iterations=99)

    assert _narrow_budget(wide, orchestrated=True) == BudgetLimit(
        max_cost_usd=2.0, max_iterations=8
    )


# --- Check-name projection over durable summaries -----------------------------


def test_check_names_keep_hook_outcomes_only() -> None:
    summaries = (
        (
            "command:run_tests: passed (exit_code=0); "
            "patch_constraints: passed (files changed: median.py); "
            "edge_cases: passed (exit_code=0)"
        ),
        "command:run_tests: failed (exit_code=1); edge_cases: failed (exit_code=1)",
    )

    assert _check_names_from_summaries(summaries) == ("edge_cases",)


def test_check_names_are_sorted_and_deduplicated() -> None:
    summaries = (
        "zeta: passed (exit_code=0); alpha: passed (exit_code=0)",
        "alpha: failed (exit_code=1)",
    )

    assert _check_names_from_summaries(summaries) == ("alpha", "zeta")


def test_check_names_are_empty_without_hook_outcomes() -> None:
    assert _check_names_from_summaries(("command:run_tests: passed (exit_code=0)",)) == ()
    assert _check_names_from_summaries(()) == ()


# --- Evidence projection over a real materialized fixture (no command runs) ----


def _materialize(binding_task_id: str, tmp_path: Path) -> tuple[BenchmarkTaskBinding, GitWorkspace]:
    binding = build_benchmark_binding(binding_task_id)
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = manager.materialize(binding.task.fixture)
    return binding, workspace


@_REQUIRES_GIT
def test_evidence_collects_hook_names_naive_solution_and_fixture_truth(tmp_path: Path) -> None:
    binding, workspace = _materialize("bench-ambiguous-success", tmp_path)
    events = (
        _verification(
            VerificationPassed,
            "command:run_tests: passed (exit_code=0); "
            "patch_constraints: passed (files changed: median.py)",
            1,
        ),
        _verification(
            VerificationFailed,
            "command:run_tests: passed (exit_code=0); "
            "patch_constraints: passed (files changed: median.py); "
            "edge_cases: failed (exit_code=1)",
            2,
        ),
        _verification(
            VerificationPassed,
            "command:run_tests: passed (exit_code=0); "
            "patch_constraints: passed (files changed: median.py); "
            "edge_cases: passed (exit_code=0)",
            3,
        ),
    )

    evidence = _evidence_for(binding, workspace, events)

    assert evidence.required_check_names == ("edge_cases",)
    assert evidence.naive_solution == AMBIGUOUS_NAIVE_SOLUTION
    assert evidence.verification_summaries == tuple(event.summary for event in events)
    fixture = binding.task.fixture
    expected = tuple(item for item in fixture.files if item.path.startswith(_TESTS_PREFIX))
    assert evidence.expected_test_files == expected
    # The untouched workspace still carries the fixture tests byte-identically.
    assert evidence.test_files == expected
    assert evidence.deleted_test_files == ()
    source_paths = {item.path for item in fixture.files if not item.path.startswith(_TESTS_PREFIX)}
    assert {item.path for item in evidence.final_sources} == source_paths
    assert evidence.final_changed_files == ()


@_REQUIRES_GIT
def test_evidence_makes_a_deleted_test_file_explicit(tmp_path: Path) -> None:
    binding, workspace = _materialize("bench-ambiguous-success", tmp_path)
    events = (
        _verification(
            VerificationPassed,
            "command:run_tests: passed (exit_code=0); edge_cases: passed (exit_code=0)",
            1,
        ),
    )
    target = workspace.root / "tests" / "test_median.py"
    target.unlink()

    evidence = _evidence_for(binding, workspace, events)

    assert evidence.deleted_test_files == ("tests/test_median.py",)
    assert tuple(item.path for item in evidence.test_files) == ("tests/__init__.py",)
    # Ground truth is the operator-owned fixture original, deletion or not.
    assert tuple(item.path for item in evidence.expected_test_files) == (
        "tests/__init__.py",
        "tests/test_median.py",
    )
    assert "tests/test_median.py" in evidence.final_changed_files


@_REQUIRES_GIT
def test_evidence_binds_no_naive_solution_outside_ambiguous_success(tmp_path: Path) -> None:
    binding, workspace = _materialize("bench-transient-api", tmp_path)
    events = (
        _verification(
            VerificationPassed,
            "command:run_tests: passed (exit_code=0); "
            "patch_constraints: passed (files changed: client.py)",
            1,
        ),
    )

    evidence = _evidence_for(binding, workspace, events)

    assert evidence.naive_solution == ()
    assert evidence.required_check_names == ()


# --- Driver failure semantics -------------------------------------------------


def test_container_task_without_image_fails_closed(tmp_path: Path) -> None:
    driver = _driver(tmp_path / "workspaces")
    spec = build_benchmark_binding("bench-simple-bug").spec

    with pytest.raises(EvalTrialError, match="no container image was wired"):
        driver(spec, _BASELINE, "baseline:bench-simple-bug:0")


def test_driver_wraps_infrastructure_crashes_with_the_trial_id(tmp_path: Path) -> None:
    def broken_factory(task: RepairTask | OrchestratedRepairTask) -> ModelPort:
        msg = "boom"
        raise RuntimeError(msg)

    driver = EvalTrialDriver(
        workspaces_root=tmp_path / "workspaces",
        model_factory=broken_factory,
        # A CONTAINER task with a wired image passes both fail-closed
        # preflights on every host; the factory then crashes before any
        # bundle (or docker daemon) is touched.
        container_image="unused:image",
        clock_factory=lambda: FixedClock(NOW),
        sleeper_factory=RecordingSleeper,
    )
    spec = build_benchmark_binding("bench-simple-bug").spec

    with pytest.raises(
        EvalTrialError,
        match=r"trial baseline:bench-simple-bug:0 failed with RuntimeError: boom",
    ):
        driver(spec, _BASELINE, "baseline:bench-simple-bug:0")


_REQUIRES_NO_RLIMIT_AS = pytest.mark.skipif(
    _rlimit_as_supported(),
    reason="trusted-sandbox platform preflight only fires where the host rejects RLIMIT_AS",
)


@_REQUIRES_NO_RLIMIT_AS
def test_driver_fails_fast_with_the_true_cause_when_trusted_sandbox_is_unsupported(
    tmp_path: Path,
) -> None:
    """Regression: the preflight names the platform cause, never a downstream ghost.

    Without it, an unsupported host spent the model's scripted turns on
    sandbox-rejected commands and surfaced "scripted model exhausted" —
    honest fail-closed, but the wrong cause. The allow side (preflight
    passes, trial succeeds) is pinned by the RLIMIT-gated trials below.
    """
    driver = _driver(tmp_path / "workspaces")
    spec = build_benchmark_binding("bench-transient-api").spec

    with pytest.raises(EvalTrialError, match=r"rejects setrlimit\(RLIMIT_AS\)") as exc_info:
        driver(spec, _BASELINE, "baseline:bench-transient-api:0")
    assert "scripted model exhausted" not in str(exc_info.value)


@_REQUIRES_NO_RLIMIT_AS
def test_driver_preflight_does_not_fire_for_container_tasks(tmp_path: Path) -> None:
    # The container fail-closed check keeps precedence on unsupported hosts:
    # a CONTAINER task without a wired image names the image, never RLIMIT.
    driver = _driver(tmp_path / "workspaces")
    spec = build_benchmark_binding("bench-simple-bug").spec

    with pytest.raises(EvalTrialError, match="no container image was wired"):
        driver(spec, _BASELINE, "baseline:bench-simple-bug:0")


# --- Approval driving loop (fully scripted runtime, no sandbox) ----------------


def _gated_metadata(name: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.LOCAL_WRITE,
        required_permission=Permission.LOCAL_WRITE,
        side_effect=SideEffectClass.LOCAL_WRITE,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NONE,
        approval=ApprovalClass.REQUIRED,
        timeout_seconds=5.0,
    )


def _approval_runtime(
    store: InMemoryEventStore,
    proposals: list[ActionProposal],
    observations: list[str],
    *,
    max_iterations: int,
) -> Runtime:
    return Runtime(
        model=ScriptedModel(proposals),
        tools=ScriptedTools(
            [ToolResult(ok=True, observation=text) for text in observations],
            metadata=[_gated_metadata("deploy")],
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=store,
        control=ControlPolicy(BudgetLimit(max_cost_usd=1.0, max_iterations=max_iterations)),
        permissions=PermissionPolicy(frozenset({Permission.READ, Permission.LOCAL_WRITE})),
        reliability=ReliabilityPolicy(),
        context=BasicContextBuilder(FixedClock(NOW)),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
    )


def _hitl_task() -> RepairTask:
    task = build_benchmark_binding("bench-hitl").task
    assert isinstance(task, RepairTask)
    return task


def test_drive_with_approvals_grants_the_pending_action_and_succeeds() -> None:
    store = InMemoryEventStore()
    runtime = _approval_runtime(
        store,
        [ActionProposal(ActionId("gated-1"), "deploy", {"target": "workspace"})],
        ["all tests pass"],
        max_iterations=5,
    )

    state = EvalTrialDriver._drive_with_approvals(  # pyright: ignore[reportPrivateUsage] - pins the driving loop directly
        runtime, _hitl_task(), _BASELINE
    )

    assert state.status is RunStatus.SUCCEEDED
    events = tuple(store.events_for(state.run_id))
    granted = [event for event in events if isinstance(event, ApprovalGranted)]
    assert len(granted) == 1
    assert granted[0].action_id == ActionId("gated-1")
    # The grant is the counted human intervention (M4 trajectory metric).
    assert count_human_interventions(events) == 1


def test_drive_with_approvals_bound_turns_a_wiring_bug_into_a_loud_error() -> None:
    store = InMemoryEventStore()
    proposals = [
        ActionProposal(ActionId(f"gated-{index}"), "deploy", {"target": "workspace"})
        for index in range(6)
    ]
    runtime = _approval_runtime(
        store,
        proposals,
        ["not quite"] * 6,  # verification never passes: the run keeps waiting
        max_iterations=50,
    )
    config = EvalConfiguration(config_id="cfg", max_cost_usd=1.0, max_iterations=3)

    with pytest.raises(EvalTrialError, match="approval grant bound"):
        EvalTrialDriver._drive_with_approvals(  # pyright: ignore[reportPrivateUsage] - pins the driving loop directly
            runtime, _hitl_task(), config
        )


# --- End-to-end trusted trials (gated like every trusted-sandbox test) --------


@_REQUIRES_GIT
@_REQUIRES_RLIMIT_AS
def test_driver_recovers_the_transient_fault_and_collects_evidence(tmp_path: Path) -> None:
    driver = _driver(tmp_path / "workspaces")
    spec = build_benchmark_binding("bench-transient-api").spec

    result = driver(spec, _BASELINE, "baseline:bench-transient-api:0")

    assert result.status is RunStatus.SUCCEEDED
    assert result.events
    assert all(event.run_id == RunId(result.run_id) for event in result.events)
    evidence = result.evidence
    assert evidence.required_check_names == ()
    assert evidence.naive_solution == ()
    assert evidence.deleted_test_files == ()
    assert evidence.expected_test_files
    # The scripted repair touches only the solution file, never the tests.
    assert evidence.test_files == evidence.expected_test_files
    assert evidence.final_sources
    assert evidence.final_changed_files
    assert evidence.verification_summaries
    # The single-runtime context builder exposes its accounting ledger (M4).
    assert len(result.context_accountings) == 1


@_REQUIRES_GIT
@_REQUIRES_RLIMIT_AS
def test_driver_outage_fault_stops_failure_with_a_durable_model_reason(
    tmp_path: Path,
) -> None:
    driver = _driver(tmp_path / "workspaces")
    spec = build_benchmark_binding("bench-provider-outage").spec

    result = driver(spec, _BASELINE, "baseline:bench-provider-outage:0")

    assert result.status is RunStatus.FAILED
    stop = result.events[-1]
    assert isinstance(stop, RunStopped)
    assert stop.reason is StopReason.FAILURE
    assert stop.summary.startswith("MODEL_UNAVAILABLE")
    # The outage strikes before any turn succeeds: no verification ran, and
    # the untouched workspace leaves tests intact with nothing deleted.
    assert result.evidence.verification_summaries == ()
    assert result.evidence.deleted_test_files == ()
    assert result.evidence.test_files == result.evidence.expected_test_files


@_REQUIRES_GIT
@_REQUIRES_RLIMIT_AS
def test_driver_uses_a_fresh_workspace_per_trial(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    driver = _driver(root)
    spec = build_benchmark_binding("bench-transient-api").spec

    first = driver(spec, _BASELINE, "baseline:bench-transient-api:0")
    second = driver(spec, _BASELINE, "baseline:bench-transient-api:1")

    assert first.status is RunStatus.SUCCEEDED
    assert second.status is RunStatus.SUCCEEDED
    assert sorted(path.name for path in root.iterdir()) == [
        "baseline_bench-transient-api_0",
        "baseline_bench-transient-api_1",
    ]


@_REQUIRES_GIT
@_REQUIRES_RLIMIT_AS
def test_driver_narrows_the_runtime_budget_from_the_configuration(tmp_path: Path) -> None:
    driver = _driver(tmp_path / "workspaces")
    spec = build_benchmark_binding("bench-transient-api").spec
    # The scripted repair needs exactly two iterations (read, then write); a
    # one-iteration configuration must stop the run loudly at the cap.
    starved = EvalConfiguration(config_id="cfg-starved", max_cost_usd=1.0, max_iterations=1)

    result = driver(spec, starved, "cfg-starved:bench-transient-api:0")

    assert result.status is RunStatus.FAILED
    stop = result.events[-1]
    assert isinstance(stop, RunStopped)
    assert stop.reason is StopReason.MAX_ITERATIONS
