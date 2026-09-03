"""PACS-016 M8: exploration-vs-no-progress tuning (the PACS-014b finding-2 fix).

The defect: every turn — including read-only exploration — triggered a full
verification; an unchanged workspace yields an identical score, so
``consecutive_no_progress`` incremented and ``STOP_STALLED_NO_PROGRESS``
killed legitimate exploration at the default limit of 3. Exploration was
indistinguishable from flailing (live evidence: six read-only turns preceded
the first successful edit in the PACS-014b validation run).

The tuning: the runtime skips verification after a turn whose completed
action is READ-only per the action's code-owned authorized ``ToolMetadata``
permission class (AGENTS.md rule 4: model output can never define or
downgrade this). No workspace change means nothing new to verify, so new
runs durably record fewer verification events — honest absence, explicable
from the stream's ``ActionAuthorized`` metadata alone. The reducer's
progress semantics are untouched; the legacy every-turn cadence stays
selectable via ``verify_read_only_turns=True`` for laboratory A/B.

The pure-runtime cadence pins run everywhere; the end-to-end pins drive the
locked ``bench-simple-bug`` benchmark fixture through the trusted local
sandbox and are capability-gated exactly like the repair end-to-end suite
(git + RLIMIT_AS).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
)
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import (
    ActionAuthorized,
    PlanCreated,
    ToolSucceeded,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy, ToolFailureClass
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
    Permission,
    RiskLevel,
    RunStatus,
    StopReason,
)
from loopforge.entrypoints.repair import (
    RepairRuntimeDeps,
    build_trusted_repair_runtime,
)
from loopforge.ports.tools import ToolResult
from loopforge.workloads.benchmarks import build_benchmark_binding
from loopforge.workloads.repair import RepairTask

NOW = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)


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

pytestmark = _REQUIRES_GIT


# --- Pure-runtime cadence pins (sandbox-free; run on every platform) -----------


def _read_metadata(name: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _write_metadata(name: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.LOCAL_WRITE,
        required_permission=Permission.LOCAL_WRITE,
        side_effect=SideEffectClass.LOCAL_WRITE,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _runtime(  # noqa: PLR0913 - keyword-only wiring keeps every runtime dependency explicit
    actions: list[ActionProposal],
    results: list[ToolResult],
    metadata: list[ToolMetadata],
    *,
    store: InMemoryEventStore | None = None,
    no_progress_limit: int = 2,
    verify_read_only_turns: bool = False,
) -> Runtime:
    return Runtime(
        model=ScriptedModel(actions),
        tools=ScriptedTools(results, metadata=metadata),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=store or InMemoryEventStore(),
        control=ControlPolicy(BudgetLimit(5.0, 10), no_progress_limit=no_progress_limit),
        permissions=PermissionPolicy(frozenset({Permission.READ, Permission.LOCAL_WRITE})),
        reliability=ReliabilityPolicy(circuit_failure_threshold=10),
        context=BasicContextBuilder(FixedClock(NOW)),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
        verify_read_only_turns=verify_read_only_turns,
    )


def _reads(count: int, *, tool: str = "inspect") -> list[ActionProposal]:
    return [ActionProposal(ActionId(f"read-{index}"), tool, {}) for index in range(count)]


@pytest.mark.parametrize(
    ("verify_read_only_turns", "expected_failed", "expected_passed"),
    [
        # Tuned default: the two reads record no verification events (honest
        # absence); only the workspace-changing write verifies, and its pass
        # grants SUCCESS_VERIFIED.
        (False, 0, 1),
        # Legacy cadence: every turn verifies; the reads record their failing
        # verdicts, and the write's pass still grants SUCCESS_VERIFIED.
        (True, 2, 1),
    ],
    ids=["tuned", "legacy"],
)
def test_read_then_successful_write_grants_success_verified_in_both_modes(
    verify_read_only_turns: bool,
    expected_failed: int,
    expected_passed: int,
) -> None:
    store = InMemoryEventStore()
    runtime = _runtime(
        [
            ActionProposal(ActionId("read-1"), "inspect", {}),
            ActionProposal(ActionId("read-2"), "inspect", {}),
            ActionProposal(ActionId("write-1"), "apply_fix", {}),
        ],
        [
            ToolResult(ok=True, observation="still broken"),
            ToolResult(ok=True, observation="still broken"),
            ToolResult(ok=True, observation="all tests pass"),
        ],
        [_read_metadata("inspect"), _write_metadata("apply_fix")],
        store=store,
        verify_read_only_turns=verify_read_only_turns,
    )

    state = runtime.run("explore then fix")

    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED
    events = store.events_for(state.run_id)
    assert sum(isinstance(event, VerificationFailed) for event in events) == expected_failed
    assert sum(isinstance(event, VerificationPassed) for event in events) == expected_passed


def test_tuned_mode_records_no_verification_events_for_read_only_turns() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(
        [
            ActionProposal(ActionId("read-1"), "inspect", {}),
            ActionProposal(ActionId("read-2"), "inspect", {}),
            ActionProposal(ActionId("write-1"), "apply_fix", {}),
        ],
        [
            ToolResult(ok=True, observation="still broken"),
            ToolResult(ok=True, observation="still broken"),
            ToolResult(ok=True, observation="all tests pass"),
        ],
        [_read_metadata("inspect"), _write_metadata("apply_fix")],
        store=store,
    )

    state = runtime.run("explore then fix")

    assert state.status is RunStatus.SUCCEEDED
    events = store.events_for(state.run_id)
    # Each read turn's tool outcome is followed directly by the re-plan that
    # moves the run out of VERIFYING — never by a verification event.
    for index, event in enumerate(events):
        if isinstance(event, ToolSucceeded) and event.action_id != ActionId("write-1"):
            assert isinstance(events[index + 1], PlanCreated)
    # Exploration accrued no no-progress strikes.
    assert state.consecutive_no_progress == 0


def test_legacy_mode_repeated_non_improving_reads_still_stall() -> None:
    runtime = _runtime(
        _reads(3),
        [ToolResult(ok=True, observation="still broken")] * 3,
        [_read_metadata("inspect")],
        no_progress_limit=2,
        verify_read_only_turns=True,
    )

    state = runtime.run("detect stall")

    assert state.status is RunStatus.STALLED
    assert state.stop_reason is StopReason.STALLED
    assert state.consecutive_no_progress == 2


def test_tuned_mode_repeated_identical_reads_stop_bounded_as_max_iterations() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(
        _reads(10),
        [ToolResult(ok=True, observation="still broken")] * 10,
        [_read_metadata("inspect")],
        store=store,
        no_progress_limit=2,
    )

    state = runtime.run("read flailing")

    # Pure-read flailing no longer accrues no-progress strikes; it stays
    # bounded by the iteration budget instead (honest consequence (a)).
    assert state.status is RunStatus.FAILED
    assert state.stop_reason is StopReason.MAX_ITERATIONS
    assert state.consecutive_no_progress == 0
    events = store.events_for(state.run_id)
    assert not [event for event in events if isinstance(event, VerificationFailed)]
    assert not [event for event in events if isinstance(event, VerificationPassed)]


@pytest.mark.parametrize("verify_read_only_turns", [False, True], ids=["tuned", "legacy"])
def test_repeated_identical_writes_still_stall_in_both_modes(
    verify_read_only_turns: bool,
) -> None:
    runtime = _runtime(
        [ActionProposal(ActionId(f"write-{index}"), "apply_fix", {}) for index in range(4)],
        [ToolResult(ok=True, observation="still broken")] * 4,
        [_write_metadata("apply_fix")],
        no_progress_limit=2,
        verify_read_only_turns=verify_read_only_turns,
    )

    state = runtime.run("write flailing")

    # Flailing detection is unharmed: workspace-changing turns verify exactly
    # as today in both cadence modes.
    assert state.status is RunStatus.STALLED
    assert state.stop_reason is StopReason.STALLED
    assert state.consecutive_no_progress == 2


def test_tuned_mode_failed_read_only_turn_still_verifies() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(
        [
            ActionProposal(ActionId("read-1"), "inspect", {}),
            ActionProposal(ActionId("write-1"), "apply_fix", {}),
        ],
        [
            ToolResult(
                ok=False,
                observation="permission denied",
                error_code="SANDBOX_POLICY",
                failure_class=ToolFailureClass.PERMANENT,
            ),
            ToolResult(ok=True, observation="all tests pass"),
        ],
        [_read_metadata("inspect"), _write_metadata("apply_fix")],
        store=store,
    )

    state = runtime.run("failed read still verifies")

    # Only SUCCESSFUL read-only turns skip verification; failure evidence and
    # retry/circuit bookkeeping are unaffected by the cadence knob.
    assert state.status is RunStatus.SUCCEEDED
    events = store.events_for(state.run_id)
    assert sum(isinstance(event, VerificationFailed) for event in events) == 1


# --- End-to-end pins on the locked bench-simple-bug fixture ---------------------


def _bench_task() -> RepairTask:
    binding = build_benchmark_binding("bench-simple-bug")
    task = binding.task
    assert isinstance(task, RepairTask)
    return task


def _exploring_actions(task: RepairTask, *, read_turns: int = 4) -> list[ActionProposal]:
    """Distinct read-only exploration turns, then the fixture's correct fix.

    Mirrors the PACS-014b live evidence (six read-only turns before the first
    edit) at the smallest scale that deterministically trips the legacy stall
    detector: four reads accrue three consecutive non-improving verifications.
    """
    solution = task.fixture.solution[0]
    probes = [
        ActionProposal(ActionId("explore-read-src"), "read_file", {"path": "slugs.py"}),
        ActionProposal(
            ActionId("explore-read-tests"), "read_file", {"path": "tests/test_slugs.py"}
        ),
        ActionProposal(ActionId("explore-search"), "search_files", {"query": "slugify"}),
        ActionProposal(ActionId("explore-status"), "workspace_status", {}),
    ]
    actions = probes[:read_turns]
    actions.append(
        ActionProposal(
            ActionId("fix-1"),
            "write_file",
            {"path": solution.path, "content": solution.content},
        )
    )
    return actions


def _deps(
    actions: list[ActionProposal], *, verify_read_only_turns: bool | None = None
) -> RepairRuntimeDeps:
    return RepairRuntimeDeps(
        store=InMemoryEventStore(),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
        model=ScriptedModel(actions),
        verify_read_only_turns=verify_read_only_turns,
    )


@_REQUIRES_RLIMIT_AS
def test_exploring_model_stalls_before_the_fix_under_legacy_cadence(tmp_path: Path) -> None:
    """The PACS-014b finding-2 defect, reproduced deterministically."""
    task = _bench_task()
    deps = _deps(_exploring_actions(task), verify_read_only_turns=True)
    bundle = build_trusted_repair_runtime(task, workspaces_dir=tmp_path, deps=deps)
    try:
        state = bundle.runtime.run(task.objective)
        events = tuple(deps.store.events_for(state.run_id))
    finally:
        bundle.close()

    assert state.status is RunStatus.STALLED
    assert state.stop_reason is StopReason.STALLED
    assert state.consecutive_no_progress == 3
    # Four read-only turns, four identical non-improving verifications — and
    # the correct fix was never even authorized.
    assert sum(isinstance(event, VerificationFailed) for event in events) == 4
    assert not [
        event
        for event in events
        if isinstance(event, ActionAuthorized) and event.proposal.tool_name == "write_file"
    ]


@_REQUIRES_RLIMIT_AS
def test_exploring_model_succeeds_under_tuned_cadence(tmp_path: Path) -> None:
    """The same exploration survives to a verifier-granted success by default."""
    task = _bench_task()
    deps = _deps(_exploring_actions(task))
    bundle = build_trusted_repair_runtime(task, workspaces_dir=tmp_path, deps=deps)
    try:
        state = bundle.runtime.run(task.objective)
        events = tuple(deps.store.events_for(state.run_id))
    finally:
        bundle.close()

    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED
    assert state.consecutive_no_progress == 0
    # Exactly one verification in the whole stream: the pass the workspace-
    # changing write earned. The four read turns recorded none.
    assert sum(isinstance(event, VerificationPassed) for event in events) == 1
    assert not [event for event in events if isinstance(event, VerificationFailed)]


@_REQUIRES_RLIMIT_AS
def test_exploring_model_with_short_exploration_succeeds_under_legacy_cadence(
    tmp_path: Path,
) -> None:
    """Success after a write still grants SUCCESS_VERIFIED in legacy mode."""
    task = _bench_task()
    deps = _deps(_exploring_actions(task, read_turns=2), verify_read_only_turns=True)
    bundle = build_trusted_repair_runtime(task, workspaces_dir=tmp_path, deps=deps)
    try:
        state = bundle.runtime.run(task.objective)
    finally:
        bundle.close()

    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED


@_REQUIRES_RLIMIT_AS
def test_read_flailing_stops_as_max_iterations_under_tuned_cadence(tmp_path: Path) -> None:
    task = _bench_task()
    flailing = [
        ActionProposal(ActionId(f"read-{index}"), "read_file", {"path": "slugs.py"})
        for index in range(8)
    ]
    deps = _deps(flailing)
    bundle = build_trusted_repair_runtime(task, workspaces_dir=tmp_path, deps=deps)
    try:
        state = bundle.runtime.run(task.objective)
        events = tuple(deps.store.events_for(state.run_id))
    finally:
        bundle.close()

    # Bounded, still honest: pure-read flailing exhausts the iteration budget
    # (8 by default) instead of stalling.
    assert state.status is RunStatus.FAILED
    assert state.stop_reason is StopReason.MAX_ITERATIONS
    assert state.iteration == 8
    assert not [
        event for event in events if isinstance(event, (VerificationFailed, VerificationPassed))
    ]


@_REQUIRES_RLIMIT_AS
@pytest.mark.parametrize("verify_read_only_turns", [False, True], ids=["tuned", "legacy"])
def test_write_flailing_still_stalls_in_both_modes(
    tmp_path: Path, verify_read_only_turns: bool
) -> None:
    task = _bench_task()
    wrong_fix = '"""Slug helpers."""\n\n\ndef slugify(text: str) -> str:\n    return text\n'
    flailing = [
        ActionProposal(
            ActionId(f"write-{index}"),
            "write_file",
            {"path": "slugs.py", "content": wrong_fix},
        )
        for index in range(5)
    ]
    deps = _deps(flailing, verify_read_only_turns=verify_read_only_turns)
    bundle = build_trusted_repair_runtime(task, workspaces_dir=tmp_path, deps=deps)
    try:
        state = bundle.runtime.run(task.objective)
    finally:
        bundle.close()

    assert state.status is RunStatus.STALLED
    assert state.stop_reason is StopReason.STALLED
    assert state.consecutive_no_progress == 3


# --- Wiring pins -----------------------------------------------------------------


def test_deps_thread_the_cadence_knob_to_the_runtime(tmp_path: Path) -> None:
    task = _bench_task()
    default_bundle = build_trusted_repair_runtime(
        task, workspaces_dir=tmp_path / "default", deps=_deps([])
    )
    try:
        assert default_bundle.runtime.verify_read_only_turns is False
    finally:
        default_bundle.close()
    legacy_bundle = build_trusted_repair_runtime(
        task,
        workspaces_dir=tmp_path / "legacy",
        deps=_deps([], verify_read_only_turns=True),
    )
    try:
        assert legacy_bundle.runtime.verify_read_only_turns is True
    finally:
        legacy_bundle.close()
