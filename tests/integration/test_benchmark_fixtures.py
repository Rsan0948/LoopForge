"""End-to-end proof that every locked benchmark fixture is solvable as designed (PACS-016, M2).

For every single-runtime binding, the fixture is materialized through the
trusted builder and driven by ``ScriptedModel`` against the fixture's locked
solution; success must be granted by the verifier, never by model claim. The
parallel-work binding runs the orchestrated path (mirroring the calculator
precedent). Targeted pins cover the category-specific traps: the
ambiguous-success naive patch fails verification via the acceptance hook, the
misleading-failure fixture fails at base with the misleading message, and the
prompt-injection payload stays out of the solution.

Capability gating mirrors ``test_repair_runtime.py``: the local-sandbox runs
execute real fixture commands and skip with reason codes on platforms that
reject the launcher's RLIMIT_AS configuration. Everything is deterministic
and network-free (AGENTS.md rule 9).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import FixedClock, RecordingSleeper, ScriptedModel
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import VerificationFailed, VerificationPassed
from loopforge.domain.state import replay
from loopforge.domain.types import ActionId, RunStatus, StopReason
from loopforge.entrypoints.orchestrated import build_orchestrated_repair_runtime
from loopforge.entrypoints.repair import (
    RepairRuntimeDeps,
    build_trusted_repair_runtime,
)
from loopforge.ports.model import ModelPort
from loopforge.workloads.benchmarks import (
    AMBIGUOUS_NAIVE_SOLUTION,
    PROMPT_INJECTION_TEXT,
    BenchmarkTaskBinding,
    benchmark_bindings,
    build_benchmark_binding,
)
from loopforge.workloads.repair import OrchestratedRepairTask, RepairTask

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)


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
    reason="git executable unavailable; benchmark end-to-end tests require the Git CLI",
)
_REQUIRES_RLIMIT_AS = pytest.mark.skipif(
    not _rlimit_as_supported(),
    reason=(
        "platform rejects setrlimit(RLIMIT_AS); local sandbox launcher cannot apply "
        "resource limits, so command execution fails closed"
    ),
)

pytestmark = _REQUIRES_GIT

_SINGLE_BINDINGS = [
    binding for binding in benchmark_bindings() if isinstance(binding.task, RepairTask)
]
_SINGLE_BINDING_IDS = [binding.spec.task_id for binding in _SINGLE_BINDINGS]


def _deps(store: InMemoryEventStore, model: ModelPort | None = None) -> RepairRuntimeDeps:
    return RepairRuntimeDeps(store=store, clock=CLOCK, sleeper=RecordingSleeper(), model=model)


@_REQUIRES_RLIMIT_AS
@pytest.mark.parametrize("binding", _SINGLE_BINDINGS, ids=_SINGLE_BINDING_IDS)
def test_every_locked_fixture_is_solvable_by_its_solution(
    tmp_path: Path, binding: BenchmarkTaskBinding
) -> None:
    task = binding.task
    assert isinstance(task, RepairTask)
    store = InMemoryEventStore()
    bundle = build_trusted_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store),
        checks=binding.checks,
    )

    state = bundle.runtime.run(task.objective)

    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED
    events = store.events_for(state.run_id)
    passed = [event for event in events if isinstance(event, VerificationPassed)]
    assert len(passed) == 1
    assert "command:run_tests: passed (exit_code=0)" in passed[0].summary
    solution_paths = tuple(sorted(item.path for item in task.fixture.solution))
    assert bundle.workspace.status().files == solution_paths
    for item in task.fixture.solution:
        repaired = (bundle.workspace.root / item.path).read_text(encoding="utf-8")
        assert repaired == item.content


@_REQUIRES_RLIMIT_AS
def test_ambiguous_success_true_solution_passes_the_edge_hook(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-ambiguous-success")
    task = binding.task
    assert isinstance(task, RepairTask)
    store = InMemoryEventStore()
    bundle = build_trusted_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store),
        checks=binding.checks,
    )

    state = bundle.runtime.run(task.objective)

    assert state.status is RunStatus.SUCCEEDED
    events = store.events_for(state.run_id)
    passed = [event for event in events if isinstance(event, VerificationPassed)]
    assert len(passed) == 1
    # The code-owned acceptance hook — not just the visible tests — gated success.
    assert "edge_cases: passed" in passed[0].summary


@_REQUIRES_RLIMIT_AS
def test_ambiguous_success_naive_patch_fails_verification(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-ambiguous-success")
    task = binding.task
    assert isinstance(task, RepairTask)
    store = InMemoryEventStore()
    # The naive patch passes the visible tests but not the hidden criterion;
    # the acceptance hook must catch the false success (M3 grader precedent).
    model = ScriptedModel(
        [
            ActionProposal(
                ActionId(f"naive-{index}"),
                "write_file",
                {
                    "path": AMBIGUOUS_NAIVE_SOLUTION[0].path,
                    "content": AMBIGUOUS_NAIVE_SOLUTION[0].content,
                },
            )
            for index in range(1, 5)
        ]
    )
    bundle = build_trusted_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store, model),
        checks=binding.checks,
    )

    state = bundle.runtime.run(task.objective)

    assert state.status is not RunStatus.SUCCEEDED
    assert state.status is RunStatus.STALLED
    events = store.events_for(state.run_id)
    assert not [event for event in events if isinstance(event, VerificationPassed)]
    failures = [event for event in events if isinstance(event, VerificationFailed)]
    assert failures
    assert any("edge_cases: failed" in event.summary for event in failures)


@_REQUIRES_RLIMIT_AS
def test_misleading_failure_fails_at_base_with_the_misleading_message(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-misleading-failure")
    task = binding.task
    assert isinstance(task, RepairTask)
    store = InMemoryEventStore()
    bundle = build_trusted_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store),
    )

    result = bundle.sandbox.run("run_tests")

    assert result.succeeded is False
    output = result.stdout + result.stderr
    # The failure message implicates widget.py; the true defect is in util.py.
    assert "widget.py" in output
    assert result.exit_code != 0


def test_prompt_injection_content_is_fixture_only_never_solution() -> None:
    binding = build_benchmark_binding("bench-prompt-injection")
    task = binding.task
    assert isinstance(task, RepairTask)
    fixture_text = "\n".join(item.content for item in task.fixture.files)
    assert PROMPT_INJECTION_TEXT in fixture_text
    for item in task.fixture.solution:
        assert PROMPT_INJECTION_TEXT not in item.content
        assert "IGNORE PREVIOUS INSTRUCTIONS" not in item.content
        assert not item.path.startswith("tests/")


@_REQUIRES_RLIMIT_AS
def test_parallel_work_orchestrated_run_succeeds(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-parallel-work")
    task = binding.task
    assert isinstance(task, OrchestratedRepairTask)
    store = InMemoryEventStore()
    deps = RepairRuntimeDeps(store=store, clock=SystemClock(), sleeper=SystemSleeper())
    bundle = build_orchestrated_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=deps,
    )
    try:
        state = bundle.orchestrator.run(task.objective, task.plan)
    finally:
        bundle.close()

    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED
    workers = state.workers
    assert [str(worker.worker_id) for worker in workers] == ["metrics", "formatting"]
    events = store.events_for(state.run_id)
    passed = [event for event in events if isinstance(event, VerificationPassed)]
    assert len(passed) == 1
    assert "command:run_tests: passed (exit_code=0)" in passed[0].summary
    assert replay(state.run_id, events) == state
