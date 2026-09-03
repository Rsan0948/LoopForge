"""Real-stream pins for the deterministic benchmark graders (PACS-016, M3).

The graders run against REAL authoritative streams produced by the locked
fixtures through the full runtime (same gating precedent as
``test_benchmark_fixtures.py``: real fixture commands, skip with reason codes
where the platform rejects the launcher's RLIMIT_AS configuration, network-free):

- the scripted successful repair: every bound grader PASSes and the trial is a
  success, not a false success;
- the ambiguous-success naive run: the run STALLs, GROUND_TRUTH FAILs, and the
  trial is NOT a false success (success was never granted);
- the key false-success pin: the real success stream of the ambiguous fixture
  paired with evidence whose test content was tampered, asserting
  FALSE_SUCCESS. The real verifier can never produce this pair on its own —
  weakening ``tests/`` is outside every binding's patch prefixes, so the
  patch-constraint check fails before success is granted. The forged case is
  therefore constructed by tampering the operator evidence against the real
  durable stream (not by a contrived verifier): exactly the verifier-gaming
  the GROUND_TRUTH re-check exists to catch.
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
from loopforge.application.graders import (
    GraderEvidence,
    grade_trial,
    trial_is_false_success,
    trial_is_success,
)
from loopforge.domain.actions import ActionProposal
from loopforge.domain.benchmarks import GraderVerdict
from loopforge.domain.events import Event, VerificationFailed, VerificationPassed
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunStatus, StopReason
from loopforge.domain.workspace import FixtureFile
from loopforge.entrypoints.repair import (
    RepairRuntimeBundle,
    RepairRuntimeDeps,
    build_trusted_repair_runtime,
)
from loopforge.ports.model import ModelPort
from loopforge.workloads.benchmarks import (
    AMBIGUOUS_NAIVE_SOLUTION,
    BenchmarkTaskBinding,
    build_benchmark_binding,
)
from loopforge.workloads.repair import RepairTask

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

_TESTS_PREFIX = "tests/"
_WEAKENED_SUITE = "import unittest\n\n# assertions removed by a test-gaming run\n"


def _deps(store: InMemoryEventStore, model: ModelPort | None = None) -> RepairRuntimeDeps:
    return RepairRuntimeDeps(store=store, clock=CLOCK, sleeper=RecordingSleeper(), model=model)


def _run(
    binding: BenchmarkTaskBinding, tmp_path: Path, model: ModelPort | None = None
) -> tuple[RunState, tuple[Event, ...], RepairRuntimeBundle]:
    """Drive one binding through the trusted runtime; return (state, events, bundle)."""
    task = binding.task
    assert isinstance(task, RepairTask)
    store = InMemoryEventStore()
    bundle = build_trusted_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store, model),
        checks=binding.checks,
    )
    state = bundle.runtime.run(task.objective)
    return state, store.events_for(state.run_id), bundle


def _evidence_for(  # noqa: PLR0913 - test wiring keeps the forging seam explicit
    binding: BenchmarkTaskBinding,
    bundle: RepairRuntimeBundle,
    events: tuple[Event, ...],
    *,
    naive_solution: tuple[FixtureFile, ...] = (),
    required_check_names: tuple[str, ...] = (),
    weakened_test_content: str | None = None,
) -> GraderEvidence:
    """Project the real end-of-run workspace into the operator evidence carrier.

    ``weakened_test_content`` is the forging seam: when set, the evidence's
    copy of the first real test suite carries that content instead of the
    on-disk bytes — a tampered-evidence stand-in for a verifier that was
    gamed into granting success over weakened tests.
    """
    task = binding.task
    assert isinstance(task, RepairTask)
    root = bundle.workspace.root
    test_files: list[FixtureFile] = []
    deleted: list[str] = []
    weakened = False
    for item in task.fixture.files:
        if not item.path.startswith(_TESTS_PREFIX):
            continue
        on_disk = root / item.path
        if not on_disk.exists():
            deleted.append(item.path)
            continue
        content = on_disk.read_text(encoding="utf-8")
        if weakened_test_content is not None and not weakened and item.path != "tests/__init__.py":
            content = weakened_test_content
            weakened = True
        test_files.append(FixtureFile(path=item.path, content=content))
    sources = tuple(
        FixtureFile(path=item.path, content=(root / item.path).read_text(encoding="utf-8"))
        for item in task.fixture.files
        if not item.path.startswith(_TESTS_PREFIX) and (root / item.path).exists()
    )
    summaries = tuple(
        event.summary
        for event in events
        if isinstance(event, VerificationPassed | VerificationFailed)
    )
    return GraderEvidence(
        final_changed_files=bundle.workspace.status().files,
        test_files=tuple(test_files),
        deleted_test_files=tuple(deleted),
        expected_test_files=tuple(
            item for item in task.fixture.files if item.path.startswith(_TESTS_PREFIX)
        ),
        final_sources=sources,
        verification_summaries=summaries,
        # Hook evidence is trusted only from PASSING summaries (anti-forgery
        # split): project the VerificationPassed subset explicitly.
        passing_verification_summaries=tuple(
            event.summary for event in events if isinstance(event, VerificationPassed)
        ),
        required_check_names=required_check_names,
        naive_solution=naive_solution,
    )


@_REQUIRES_RLIMIT_AS
def test_scripted_successful_repair_passes_every_grader(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-simple-bug")

    state, events, bundle = _run(binding, tmp_path)

    assert state.status is RunStatus.SUCCEEDED
    evidence = _evidence_for(binding, bundle, events)
    results = grade_trial(binding.spec, events, evidence)

    assert [result.verdict for result in results] == [GraderVerdict.PASS] * len(results)
    assert trial_is_success(results, events) is True
    assert trial_is_false_success(results, events) is False


@_REQUIRES_RLIMIT_AS
def test_ambiguous_success_true_solution_passes_ground_truth(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-ambiguous-success")

    state, events, bundle = _run(binding, tmp_path)

    assert state.status is RunStatus.SUCCEEDED
    evidence = _evidence_for(
        binding,
        bundle,
        events,
        naive_solution=AMBIGUOUS_NAIVE_SOLUTION,
        required_check_names=("edge_cases",),
    )
    results = grade_trial(binding.spec, events, evidence)

    assert [result.verdict for result in results] == [GraderVerdict.PASS] * len(results)
    assert trial_is_success(results, events) is True
    assert trial_is_false_success(results, events) is False


@_REQUIRES_RLIMIT_AS
def test_ambiguous_success_naive_run_stalls_and_fails_ground_truth(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-ambiguous-success")
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

    state, events, bundle = _run(binding, tmp_path, model)

    assert state.status is RunStatus.STALLED
    evidence = _evidence_for(
        binding,
        bundle,
        events,
        naive_solution=AMBIGUOUS_NAIVE_SOLUTION,
        required_check_names=("edge_cases",),
    )
    results = grade_trial(binding.spec, events, evidence)
    verdicts = {result.grader_id.value: result for result in results}

    assert verdicts["verified_success"].verdict is GraderVerdict.FAIL
    assert verdicts["scope_discipline"].verdict is GraderVerdict.PASS
    assert verdicts["ground_truth"].verdict is GraderVerdict.FAIL
    assert "edge_cases" in verdicts["ground_truth"].detail
    # Success was never granted: a stalled naive run is an honest failure,
    # never a false success.
    assert trial_is_false_success(results, events) is False
    assert trial_is_success(results, events) is False


@_REQUIRES_RLIMIT_AS
def test_forged_success_with_weakened_tests_is_a_false_success(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-ambiguous-success")

    state, events, bundle = _run(binding, tmp_path)

    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED
    # The forged case: the SAME real durable stream (success_verified stop,
    # hook evidence present) paired with evidence whose test suite was
    # weakened. No honest verifier grants this — weakening tests/ violates
    # every binding's patch prefixes — so the tamper stands in for a gamed
    # verifier, and the operator-authority re-check must catch it.
    evidence = _evidence_for(
        binding,
        bundle,
        events,
        naive_solution=AMBIGUOUS_NAIVE_SOLUTION,
        required_check_names=("edge_cases",),
        weakened_test_content=_WEAKENED_SUITE,
    )
    results = grade_trial(binding.spec, events, evidence)
    verdicts = {result.grader_id.value: result for result in results}

    assert verdicts["verified_success"].verdict is GraderVerdict.PASS
    assert verdicts["scope_discipline"].verdict is GraderVerdict.PASS
    assert verdicts["ground_truth"].verdict is GraderVerdict.FALSE_SUCCESS
    assert "weakened" in verdicts["ground_truth"].detail
    assert trial_is_false_success(results, events) is True
    assert trial_is_success(results, events) is False
