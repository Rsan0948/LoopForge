"""Live multi-trial benchmark validation — separated from deterministic CI.

Everything in this directory requires real external services (a running Ollama
server with the pinned model, a live Docker daemon) and skips with explicit
reason codes when they are unavailable, so deterministic CI remains runnable
with zero provider credentials and zero live infrastructure. Nondeterministic
model behavior must never leak into the deterministic suites.

This file is the PACS-016 (M10) live validation: the focused live matrix —
bench-simple-bug, bench-misleading-failure, bench-stall x the code-owned
``baseline`` and ``tight-budget`` configuration presets x 2 trials (twelve live
container trials) — driven by the production ``EvalTrialDriver`` wired exactly
as the M6 CLI wires ``eval --model ollama`` (per-trial ``build_ollama_model``
with the registered context window, STANDARD tier, the house container image).
Assertions are STRUCTURAL INVARIANTS ONLY — terminal streams, full grader
coverage, report/store round-trip — never a specific success rate: live-model
outcomes are genuinely nondeterministic, and the captured stdout table is the
self-documenting evidence for the cycle record.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Final

import pytest
from pydantic import BaseModel, ConfigDict

from loopforge.application.eval_runner import (
    EvalConfiguration,
    TrialRunResult,
    run_trials,
)
from loopforge.application.graders import grade_trial, trial_is_success
from loopforge.domain.benchmarks import BenchmarkReport, BenchmarkTaskSpec, GraderResult
from loopforge.domain.events import RunStopped
from loopforge.domain.routing import ModelTier
from loopforge.entrypoints.cli import build_ollama_model
from loopforge.entrypoints.eval import (
    EvalReportStore,
    EvalTrialDriver,
    resolve_configurations,
)
from loopforge.ports.model import ModelPort
from loopforge.workloads.benchmarks import benchmark_suite, build_benchmark_binding
from loopforge.workloads.repair import OrchestratedRepairTask, RepairTask

_OLLAMA_URL = "http://localhost:11434"
_OLLAMA_MODEL = "devstral-small-2:latest"
# Honest context window of the deployed model, registered as routing
# capability metadata — mirrors the CLI's --ollama-context-window default.
_OLLAMA_CONTEXT_WINDOW_TOKENS: Final = 131_072
_TEST_IMAGE = "python:3.12-alpine"

_LIVE_TASK_IDS: Final = ("bench-simple-bug", "bench-misleading-failure", "bench-stall")
_LIVE_CONFIG_IDS: Final = ("baseline", "tight-budget")
_TRIALS_PER_TASK: Final = 2
_REPORT_ID: Final = "live-pacs016-m10-matrix"


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
        f"`ollama pull {_OLLAMA_MODEL}` to execute the live benchmark eval matrix"
    ),
)

_REQUIRES_CONTAINER = pytest.mark.skipif(
    not _container_ready(),
    reason=(
        f"docker daemon or {_TEST_IMAGE} test image unavailable; start Docker and run "
        f"`docker pull {_TEST_IMAGE}` to execute the live benchmark eval matrix"
    ),
)


def _ollama_model_factory(task: RepairTask | OrchestratedRepairTask) -> ModelPort:
    """Per-trial live model, mirroring the M6 CLI's ``--model ollama`` wiring.

    Same seam as ``cli._eval_model_factory``: one fresh adapter per trial, the
    operator-owned context window registered as capability metadata; for the
    orchestrated binding the tool catalog comes from the first worker
    assignment (all three tasks in this matrix are single-runtime, but the
    factory honors the driver's full contract).
    """
    spec_task = task if isinstance(task, RepairTask) else task.assignments[0].task
    return build_ollama_model(
        spec_task,
        model_name=_OLLAMA_MODEL,
        base_url=_OLLAMA_URL,
        context_window_tokens=_OLLAMA_CONTEXT_WINDOW_TOKENS,
    )


class _RecordingDriver:
    """``TrialDriver`` wrapper capturing each trial's inputs and result.

    ``run_trials`` aggregates internally; the structural assertions below need
    the per-trial streams and evidence, so the wrapper records exactly what it
    passed through — the production ``EvalTrialDriver`` stays untouched.
    """

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
        f"{'config':<14} {'task':<26} {'trials':>6} {'succ':>5} "
        f"{'false-succ':>10} {'cost':>10} {'latency':>9} {'interv':>7}"
    )
    for entry in report.config_reports:
        print(
            f"{entry.config_id:<14} {entry.task_id:<26} {entry.trials:>6} "
            f"{entry.successes:>5} {entry.false_successes:>10} "
            f"${entry.mean_cost_usd:>9.4f} {entry.mean_latency_seconds:>8.2f}s "
            f"{entry.mean_human_interventions:>7.2f}"
        )
    print(f"pareto frontier: {', '.join(report.pareto_config_ids)}")
    print(f"report saved: {saved_path}")


@_REQUIRES_OLLAMA
@_REQUIRES_CONTAINER
def test_live_benchmark_eval_matrix(tmp_path: Path) -> None:
    """PACS-016 M10: the focused live matrix through the production eval stack."""
    suite = benchmark_suite()
    # Subset resolution mirrors cli._eval_specs: named ids resolved against
    # the locked bindings; the report echoes the FULL-suite version + lock.
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

    # Every trial produced a TrialRecord: run_trials builds one domain-
    # validated record per trial (its constructor rejects invalid content),
    # and the cross-check below recomputes the same per-trial classifications
    # from the captured streams, so any record-level inconsistency fails here.
    graded_by_trial: dict[
        str, tuple[BenchmarkTaskSpec, EvalConfiguration, TrialRunResult, tuple[GraderResult, ...]]
    ] = {}
    for spec, config, trial_id, result in driver.trials:
        events = result.events
        assert events, f"trial {trial_id} produced no durable events"
        # Terminal state per the events: RunStopped present and LAST — the
        # same stream-terminality rule every M3 grader applies.
        assert isinstance(events[-1], RunStopped), (
            f"trial {trial_id} stream is non-terminal: last event {type(events[-1]).__name__}"
        )
        graded = grade_trial(spec, events, result.evidence)
        # Grader coverage: every grader bound by the locked spec produced a
        # verdict, in binding order.
        assert tuple(entry.grader_id for entry in graded) == spec.grader_ids
        graded_by_trial[trial_id] = (spec, config, result, tuple(graded))

    # The report validates (it constructed) and covers the full matrix.
    assert len(report.config_reports) == len(expected_pairs)
    assert {(entry.config_id, entry.task_id) for entry in report.config_reports} == expected_pairs
    assert report.suite_version == suite.version
    assert report.lock_hash == suite.lock_hash

    # Aggregation cross-check: each (config, task) ConfigReport's counts agree
    # with the captured trials' recomputed classifications.
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

    # False success is a HARD INVARIANT here, not a live-model luck call: the
    # bound graders are VERIFIED_SUCCESS + SCOPE_DISCIPLINE (+ RECOVERY for
    # stall). VERIFIED_SUCCESS is the success-claim check itself — it passes
    # whenever success is durably claimed, so it can never diverge. SCOPE_
    # DISCIPLINE re-applies the exact PatchConstraints semantics the verifier
    # enforced before granting success, and no turn executes after the
    # granting verification, so the end-of-run scope re-check reads the same
    # workspace the verifier approved (a scope violation + success means the
    # verifier's own patch constraints would have failed first). RECOVERY on
    # STALL passes whenever the run solved. A false success here therefore
    # requires a verifier/grader DIVERGENCE — a harness defect, which must
    # fail this validation loudly rather than be recorded as evidence.
    assert all(entry.false_successes == 0 for entry in report.config_reports)

    # The report round-trips the operator-owned store exactly.
    assert loaded == report

    _print_evidence_table(report, saved_path=saved_path)
