"""End-to-end pins for the M5 fault-injection adapters and eval runner (PACS-016).

REAL streams through ``build_trusted_repair_runtime`` with ``ScriptedModel``
wrapped in the M5 fault decorators (same gating precedent as
``test_benchmark_graders.py``: real fixture commands, skipped with reason
codes where the platform rejects the launcher's RLIMIT_AS configuration,
network-free):

- ``OutageModel`` on ``bench-provider-outage``: the bounded retry streak
  cannot outlast the outage, so the runtime stops FAILURE with a durable
  ``MODEL_*`` reason; the RECOVERY grader PASSes (graceful degradation) and
  the trial is neither a success nor a false success;
- ``TransientFailureModel(failures=2)`` on ``bench-transient-api``: the run
  recovers (transient retries never burn iterations) and SUCCEEDs; RECOVERY
  PASSes and the trial is a genuine success;
- a full runner-level loop — 2 configs x 2 tasks x 2 trials — driven by a
  small test-local driver that mirrors what M6's entrypoints driver will do
  (build the locked binding, wrap the scripted model per the binding's fault
  descriptor, drive, collect evidence from the workspace + events), pinning
  the aggregated ``BenchmarkReport`` exactly. Keeping this driver in the test
  is deliberate: it is the M6 wiring's rehearsal, and asserting against real
  streams pins the runner's aggregation without any canned data.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.fault_models import OutageModel, TransientFailureModel
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import FixedClock, RecordingSleeper, ScriptedModel
from loopforge.application.eval_runner import (
    EvalConfiguration,
    TrialRunResult,
    run_trials,
)
from loopforge.application.graders import (
    GraderEvidence,
    grade_trial,
    trial_is_false_success,
    trial_is_success,
)
from loopforge.domain.benchmarks import BenchmarkTaskSpec, GraderVerdict, suite_lock_hash
from loopforge.domain.events import (
    Event,
    RunStopped,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.types import BudgetLimit, RunStatus, StopReason
from loopforge.domain.workspace import FixtureFile
from loopforge.entrypoints.repair import (
    RepairRuntimeBundle,
    RepairRuntimeDeps,
    build_trusted_repair_runtime,
)
from loopforge.ports.model import ModelPort
from loopforge.workloads.benchmarks import (
    BENCHMARK_SUITE_VERSION,
    BenchmarkFaultKind,
    BenchmarkTaskBinding,
    build_benchmark_binding,
)
from loopforge.workloads.repair import RepairTask, scripted_repair_actions

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)

_TESTS_PREFIX = "tests/"


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

pytestmark = [_REQUIRES_GIT, _REQUIRES_RLIMIT_AS]


def _wrap_for_fault(binding: BenchmarkTaskBinding, model: ModelPort) -> ModelPort:
    """Mirror the M6 wiring: map the binding's fault descriptor to a decorator."""
    fault = binding.fault
    if fault is None:
        return model
    if fault.kind is BenchmarkFaultKind.PROVIDER_OUTAGE:
        return OutageModel(model)
    if fault.kind is BenchmarkFaultKind.TRANSIENT_API_FAILURE:
        return TransientFailureModel(model, fault.transient_failure_count)
    msg = f"unknown benchmark fault kind: {fault.kind!r}"
    raise ValueError(msg)


def _drive(
    binding: BenchmarkTaskBinding,
    workspaces_dir: Path,
    *,
    budget: BudgetLimit | None = None,
    no_progress_limit: int | None = None,
) -> tuple[TrialRunResult, RepairRuntimeBundle]:
    """Build, fault-wrap, and drive one binding; return the raw trial outcome."""
    task = binding.task
    assert isinstance(task, RepairTask)
    store = InMemoryEventStore()
    model = _wrap_for_fault(binding, ScriptedModel(scripted_repair_actions(task)))
    deps = RepairRuntimeDeps(
        store=store,
        clock=CLOCK,
        sleeper=RecordingSleeper(),
        model=model,
        budget=budget,
        no_progress_limit=no_progress_limit,
    )
    bundle = build_trusted_repair_runtime(
        task,
        workspaces_dir=workspaces_dir,
        deps=deps,
        checks=binding.checks,
    )
    state = bundle.runtime.run(task.objective)
    events = store.events_for(state.run_id)
    result = TrialRunResult(
        run_id=str(state.run_id),
        status=state.status,
        events=events,
        evidence=_evidence_for(binding, bundle, events),
    )
    return result, bundle


def _evidence_for(
    binding: BenchmarkTaskBinding,
    bundle: RepairRuntimeBundle,
    events: tuple[Event, ...],
) -> GraderEvidence:
    """Project the real end-of-run workspace into the operator evidence carrier."""
    task = binding.task
    assert isinstance(task, RepairTask)
    root = bundle.workspace.root
    test_files: list[FixtureFile] = []
    deleted: list[str] = []
    for item in task.fixture.files:
        if not item.path.startswith(_TESTS_PREFIX):
            continue
        on_disk = root / item.path
        if not on_disk.exists():
            deleted.append(item.path)
            continue
        test_files.append(FixtureFile(path=item.path, content=on_disk.read_text(encoding="utf-8")))
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
    )


def test_outage_fault_stops_failure_with_durable_model_reason(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-provider-outage")

    result, bundle = _drive(binding, tmp_path / "workspaces")
    try:
        assert result.status is RunStatus.FAILED
        stop = result.events[-1]
        assert isinstance(stop, RunStopped)
        assert stop.reason is StopReason.FAILURE
        assert stop.summary.startswith("MODEL_UNAVAILABLE")

        results = grade_trial(binding.spec, result.events, result.evidence)
        verdicts = {entry.grader_id.value: entry for entry in results}
        assert verdicts["verified_success"].verdict is GraderVerdict.FAIL
        assert verdicts["scope_discipline"].verdict is GraderVerdict.PASS
        # Graceful degradation: terminal model-failure stop, no fabricated success.
        assert verdicts["recovery"].verdict is GraderVerdict.PASS
        assert trial_is_success(results, result.events) is False
        assert trial_is_false_success(results, result.events) is False
    finally:
        bundle.close()


def test_transient_fault_recovers_and_succeeds(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-transient-api")

    result, bundle = _drive(binding, tmp_path / "workspaces")
    try:
        assert result.status is RunStatus.SUCCEEDED

        results = grade_trial(binding.spec, result.events, result.evidence)
        verdicts = {entry.grader_id.value: entry for entry in results}
        assert verdicts["verified_success"].verdict is GraderVerdict.PASS
        assert verdicts["scope_discipline"].verdict is GraderVerdict.PASS
        assert verdicts["recovery"].verdict is GraderVerdict.PASS
        assert trial_is_success(results, result.events) is True
        assert trial_is_false_success(results, result.events) is False
    finally:
        bundle.close()


class _ScriptedBenchmarkDriver:
    """Test-local rehearsal of the M6 entrypoints driver.

    Builds the locked binding for the spec, wraps the scripted model per the
    binding's fault descriptor, wires the runtime budget from the
    ``EvalConfiguration``, drives the run, and collects the operator evidence
    from the end-of-run workspace and durable events.
    """

    def __init__(self, workspaces_root: Path) -> None:
        self._workspaces_root = workspaces_root

    def __call__(
        self, spec: BenchmarkTaskSpec, config: EvalConfiguration, trial_id: str
    ) -> TrialRunResult:
        binding = build_benchmark_binding(spec.task_id)
        result, bundle = _drive(
            binding,
            self._workspaces_root / trial_id.replace(":", "_"),
            budget=BudgetLimit(
                max_cost_usd=config.max_cost_usd,
                max_iterations=config.max_iterations,
            ),
            no_progress_limit=config.no_progress_limit,
        )
        bundle.close()
        return result


def test_runner_loop_over_fault_tasks_aggregates_exactly(tmp_path: Path) -> None:
    bindings = (
        build_benchmark_binding("bench-provider-outage"),
        build_benchmark_binding("bench-transient-api"),
    )
    specs = tuple(binding.spec for binding in bindings)
    configs = (
        EvalConfiguration(config_id="cfg-baseline", max_cost_usd=1.0, max_iterations=8),
        # A second, narrower configuration: deterministic outcomes are
        # identical, so the two configs tie and BOTH stay on the frontier.
        EvalConfiguration(config_id="cfg-narrow", max_cost_usd=0.5, max_iterations=4),
    )
    driver = _ScriptedBenchmarkDriver(tmp_path / "workspaces")

    report = run_trials(
        specs,
        configs,
        2,
        driver,
        report_id="report-faults",
        suite_version=BENCHMARK_SUITE_VERSION,
        lock_hash=suite_lock_hash(specs),
    )

    assert report.report_id == "report-faults"
    assert report.suite_version == BENCHMARK_SUITE_VERSION
    assert report.lock_hash == suite_lock_hash(specs)
    assert [(entry.config_id, entry.task_id) for entry in report.config_reports] == [
        ("cfg-baseline", "bench-provider-outage"),
        ("cfg-baseline", "bench-transient-api"),
        ("cfg-narrow", "bench-provider-outage"),
        ("cfg-narrow", "bench-transient-api"),
    ]
    for entry in report.config_reports:
        assert entry.trials == 2
        assert entry.false_successes == 0
        assert entry.false_success_rate == 0.0
        assert entry.mean_human_interventions == 0.0
        # Fixed clock: every durable timestamp is identical, so latency is 0.
        assert entry.mean_latency_seconds == 0.0
        if entry.task_id == "bench-provider-outage":
            # The outage stops before any successful turn: no debits at all.
            assert entry.successes == 0
            assert entry.success_rate == 0.0
            assert entry.mean_cost_usd == 0.0
            assert entry.mean_total_tokens == 0.0
        else:
            # Recovered repair: read + write turns (0.01 each, 120 tokens each).
            assert entry.successes == 2
            assert entry.success_rate == 1.0
            assert entry.mean_cost_usd == 0.02
            assert entry.mean_total_tokens == 240.0
    # Identical outcome vectors tie; ties never dominate, so both configs stay.
    assert report.pareto_config_ids == ("cfg-baseline", "cfg-narrow")
