"""Live PACS-017 M10 validation — separated from deterministic CI.

Everything in this directory requires real external services (a running Ollama
server with the pinned model, a live Docker daemon) and skips with explicit
reason codes when they are unavailable, so deterministic CI remains runnable
with zero provider credentials and zero live infrastructure. Nondeterministic
model behavior must never leak into the deterministic suites.

This file is the PACS-017 (M10) live validation, mirroring the PACS-016 M10
harness but across EXECUTION POLICIES instead of budget presets:

- the focused live matrix — bench-simple-bug, bench-misleading-failure,
  bench-stall x the code-owned ``policy-baseline`` and
  ``policy-adaptive-context`` configuration arms x 2 trials (twelve live
  container trials) — driven by the production ``EvalTrialDriver`` with the
  M5 per-trial ``_policy_for`` wiring, proving an adaptive candidate is
  benchmarked against the locked suite with zero false successes;
- a live SHADOWED container run: the candidate advises through the wired
  ``CandidateShadowAdvisor`` while the active policy executes — shadow
  decisions are journaled for audit and never enacted.

Assertions are STRUCTURAL INVARIANTS ONLY — terminal streams, full grader
coverage, policy arms resolved per trial, report/store round-trip — never a
specific success rate: live-model outcomes are genuinely nondeterministic,
and the captured stdout table is the self-documenting evidence for the
cycle record.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Final

import pytest
from pydantic import BaseModel, ConfigDict

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.eval_runner import (
    EvalConfiguration,
    TrialRunResult,
    run_trials,
)
from loopforge.application.graders import grade_trial, trial_is_success
from loopforge.domain.benchmarks import BenchmarkReport, BenchmarkTaskSpec, GraderResult
from loopforge.domain.events import ArtifactRecorded, RunStopped, ShadowDecisionRecorded
from loopforge.domain.policies import ADAPTIVE_CONTEXT_POLICY
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.routing import ModelTier
from loopforge.domain.types import RunStatus
from loopforge.entrypoints.cli import build_ollama_model
from loopforge.entrypoints.eval import (
    EvalReportStore,
    EvalTrialDriver,
    resolve_configurations,
)
from loopforge.entrypoints.repair import RepairRuntimeDeps, build_container_repair_runtime
from loopforge.ports.model import ModelPort
from loopforge.workloads.benchmarks import benchmark_suite, build_benchmark_binding
from loopforge.workloads.fixtures import adder_repair_task
from loopforge.workloads.repair import OrchestratedRepairTask, RepairTask, repair_tool_specs

_OLLAMA_URL = "http://localhost:11434"
_OLLAMA_MODEL = "devstral-small-2:latest"
# Honest context window of the deployed model, registered as routing
# capability metadata — mirrors the CLI's --ollama-context-window default.
_OLLAMA_CONTEXT_WINDOW_TOKENS: Final = 131_072
_TEST_IMAGE = "python:3.12-alpine"

_LIVE_TASK_IDS: Final = ("bench-simple-bug", "bench-misleading-failure", "bench-stall")
_LIVE_CONFIG_IDS: Final = ("policy-baseline", "policy-adaptive-context")
_TRIALS_PER_TASK: Final = 2
_REPORT_ID: Final = "live-pacs017-m10-policy-matrix"


class _TagEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class _TagsProbe(BaseModel):
    models: list[_TagEntry] = []


def _ollama_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{_OLLAMA_URL}/api/tags", timeout=5) as response:
            tags = _TagsProbe.model_validate(json.load(response))
    except (OSError, ValueError):
        return False
    return any(item.name == _OLLAMA_MODEL for item in tags.models)


def _image_available() -> bool:
    for reference in (_TEST_IMAGE, f"docker.io/library/{_TEST_IMAGE}"):
        try:
            inspect = subprocess.run(
                ["docker", "image", "inspect", reference],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if inspect.returncode == 0:
            return True
    return False


def _container_ready() -> bool:
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and _image_available()


_REQUIRES_OLLAMA = pytest.mark.skipif(
    not _ollama_ready(),
    reason=(
        f"ollama server or {_OLLAMA_MODEL} model unavailable; start Ollama and run "
        f"`ollama pull {_OLLAMA_MODEL}` to execute the live policy eval matrix"
    ),
)

_REQUIRES_CONTAINER = pytest.mark.skipif(
    not _container_ready(),
    reason=(
        f"docker daemon or {_TEST_IMAGE} test image unavailable; start Docker and run "
        f"`docker pull {_TEST_IMAGE}` to execute the live policy eval matrix"
    ),
)


def _ollama_model_factory(task: RepairTask | OrchestratedRepairTask) -> ModelPort:
    """Per-trial live model, mirroring the CLI's ``--model ollama`` wiring."""
    spec_task = task if isinstance(task, RepairTask) else task.assignments[0].task
    return build_ollama_model(
        spec_task,
        model_name=_OLLAMA_MODEL,
        base_url=_OLLAMA_URL,
        context_window_tokens=_OLLAMA_CONTEXT_WINDOW_TOKENS,
    )


class _RecordingDriver:
    """``TrialDriver`` wrapper capturing each trial's inputs and result."""

    def __init__(self, driver: EvalTrialDriver) -> None:
        self._driver = driver
        self.trials: list[tuple[BenchmarkTaskSpec, EvalConfiguration, str, TrialRunResult]] = []

    def __call__(
        self, spec: BenchmarkTaskSpec, config: EvalConfiguration, trial_id: str
    ) -> TrialRunResult:
        result = self._driver(spec, config, trial_id)
        self.trials.append((spec, config, trial_id, result))
        return result


def _print_evidence_table(report: BenchmarkReport, *, saved_path: Path) -> None:
    """Self-documenting per-(config, task) evidence table for the cycle record."""
    task_ids = sorted({entry.task_id for entry in report.config_reports})
    print(
        f"live eval report={report.report_id} suite={report.suite_version} "
        f"lock={report.lock_hash[:12]}"
    )
    print(f"tasks covered ({len(task_ids)}): {', '.join(task_ids)}")
    print(
        f"{'config':<24} {'task':<26} {'trials':>6} {'succ':>5} {'false-succ':>10} "
        f"{'cost':>10} {'latency':>9} {'ctx-tok':>8} {'dropped':>8} {'recovery':>9}"
    )
    for entry in report.config_reports:
        print(
            f"{entry.config_id:<24} {entry.task_id:<26} {entry.trials:>6} "
            f"{entry.successes:>5} {entry.false_successes:>10} "
            f"${entry.mean_cost_usd:>9.4f} {entry.mean_latency_seconds:>8.2f}s "
            f"{entry.mean_context_tokens_used:>8.0f} {entry.mean_context_items_dropped:>8.2f} "
            f"{entry.mean_recovery_events:>9.2f}"
        )
    print(f"pareto frontier: {', '.join(report.pareto_config_ids)}")
    print(f"report saved: {saved_path}")


@_REQUIRES_OLLAMA
@_REQUIRES_CONTAINER
def test_live_policy_eval_matrix(tmp_path: Path) -> None:
    """PACS-017 M10: two execution policies benchmarked on the locked suite.

    The acceptance gate's "quality, cost, latency, context, recovery, and
    human-intervention tradeoffs across at least two execution policies":
    the baseline arm runs the pre-PACS-017 wiring (fixed 4096 envelope),
    the candidate arm runs the adaptive allocator inside policy bounds —
    both through the same production driver, graded by the same locked
    bindings.
    """
    suite = benchmark_suite()
    specs = tuple(build_benchmark_binding(task_id).spec for task_id in _LIVE_TASK_IDS)
    configs = resolve_configurations(_LIVE_CONFIG_IDS)
    driver = _RecordingDriver(
        EvalTrialDriver(
            workspaces_root=tmp_path / "workspaces",
            model_factory=_ollama_model_factory,
            # Live models register at STANDARD, matching the CLI wiring.
            model_tier=ModelTier.STANDARD,
            container_image=_TEST_IMAGE,
        )
    )

    report = run_trials(
        specs,
        configs,
        _TRIALS_PER_TASK,
        driver,
        report_id=_REPORT_ID,
        suite_version=suite.version,
        lock_hash=suite.lock_hash,
    )

    store = EvalReportStore(tmp_path / "evals")
    saved_path = store.save(report)
    loaded = store.load(report.report_id)

    # --- Structural invariants (never a live-model success rate). -----------
    expected_pairs = {(config.config_id, spec.task_id) for config in configs for spec in specs}
    assert len(driver.trials) == len(expected_pairs) * _TRIALS_PER_TASK

    # Both arms carry their versioned policy reference — the per-trial
    # ``_policy_for`` resolution (fail-closed against the built-in registry)
    # is what makes this a POLICY comparison, not two budget presets.
    assert all(config.policy_id is not None for config in configs)
    assert {config.config_id for config in configs} == set(_LIVE_CONFIG_IDS)

    graded_by_trial: dict[
        str, tuple[BenchmarkTaskSpec, EvalConfiguration, TrialRunResult, tuple[GraderResult, ...]]
    ] = {}
    for spec, config, trial_id, result in driver.trials:
        events = result.events
        assert events, f"trial {trial_id} produced no durable events"
        assert isinstance(events[-1], RunStopped), (
            f"trial {trial_id} stream is non-terminal: last event {type(events[-1]).__name__}"
        )
        graded = grade_trial(spec, events, result.evidence)
        assert tuple(entry.grader_id for entry in graded) == spec.grader_ids
        graded_by_trial[trial_id] = (spec, config, result, tuple(graded))

    assert len(report.config_reports) == len(expected_pairs)
    assert {(entry.config_id, entry.task_id) for entry in report.config_reports} == expected_pairs
    assert report.suite_version == suite.version
    assert report.lock_hash == suite.lock_hash

    for entry in report.config_reports:
        group = [
            item
            for item in graded_by_trial.values()
            if item[1].config_id == entry.config_id and item[0].task_id == entry.task_id
        ]
        assert entry.trials == len(group) == _TRIALS_PER_TASK
        successes = sum(
            1 for _spec, _config, result, graded in group if trial_is_success(graded, result.events)
        )
        assert entry.successes == successes

    # False success is a HARD INVARIANT (verifier/grader divergence = harness
    # defect), never live-model luck — see the PACS-016 M10 matrix for the
    # full argument; the same locked bindings apply here.
    assert all(entry.false_successes == 0 for entry in report.config_reports)

    # The v2 axes are MEASURED on the live runs (not zero-filled): the
    # context axis feeds the M8 heuristics, the recovery axis the stall knob.
    assert all(entry.mean_context_tokens_used > 0.0 for entry in report.config_reports)

    assert loaded == report

    _print_evidence_table(report, saved_path=saved_path)


@_REQUIRES_OLLAMA
@_REQUIRES_CONTAINER
def test_live_shadowed_container_run_journals_evidence_without_enacting(
    tmp_path: Path,
) -> None:
    """PACS-017 M10: a live shadowed run through the container composition root.

    The candidate (``adaptive-context`` v1) advises through the wired
    ``CandidateShadowAdvisor`` while the active policy executes: its decisions
    are journaled as ``ShadowDecisionRecorded`` audit evidence against the
    durable SQLite stream, and the active repair is granted solely by the
    provider-independent verifier stack — the shadow never enacts.
    """
    task = adder_repair_task(executable="/usr/local/bin/python")
    model = OllamaModel(
        model=_OLLAMA_MODEL,
        tools=repair_tool_specs(task),
        template=default_controller_template(),
        base_url=_OLLAMA_URL,
    )
    store = SQLiteEventStore(tmp_path / "events.db", codec=JsonEventCodec())
    deps = RepairRuntimeDeps(
        store=store,
        clock=SystemClock(),
        sleeper=SystemSleeper(),
        telemetry=InMemoryTelemetry(),
        model=model,
        model_tier=ModelTier.STANDARD,
        shadow_policy=ADAPTIVE_CONTEXT_POLICY,
    )
    bundle = build_container_repair_runtime(
        task,
        image=_TEST_IMAGE,
        workspaces_dir=tmp_path / "workspaces",
        deps=deps,
    )
    try:
        state = bundle.runtime.run(task.objective)
    finally:
        bundle.close()

    # Success is granted solely by the verifier stack, exactly as unshadowed.
    assert state.status is RunStatus.SUCCEEDED
    assert state.last_verification is not None
    assert "command:run_tests: passed (exit_code=0)" in state.last_verification

    events = store.events_for(state.run_id)
    shadowed = [event for event in events if isinstance(event, ShadowDecisionRecorded)]
    assert shadowed, "the wired advisor must journal shadow decisions on a live run"
    assert {event.policy_id for event in shadowed} == {ADAPTIVE_CONTEXT_POLICY.policy_id}
    assert {event.policy_version for event in shadowed} == {ADAPTIVE_CONTEXT_POLICY.version}

    # Never enacted: the exact patch landed through the ACTIVE path only.
    artifacts = [event for event in events if isinstance(event, ArtifactRecorded)]
    assert artifacts, "the exact patch must be recorded as durable evidence"
    assert "+    return left + right" in artifacts[-1].content

    print(
        f"live shadowed run={state.run_id} status={state.status.value} "
        f"shadow_decisions={len(shadowed)} "
        f"kinds={sorted({event.kind.value for event in shadowed})}"
    )
