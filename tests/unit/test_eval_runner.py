"""Unit pins for the M5 multi-trial eval runner (PACS-016).

Stub drivers return canned ``TrialRunResult``s so the runner's own logic is
pinned without any runtime: aggregation math (rates/means), success vs
false-success counting including the forged case, Pareto frontier semantics
(dominated excluded, tied retained, tradeoffs co-frontier) plus the uniform
cross-product task coverage that makes dominance commensurable, deterministic
trial ids, runner validation allow+deny (AGENTS.md rule 10) including the
driver run-id stream contract, and latency derivation for terminal and
non-terminal streams.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from loopforge.application.eval_runner import (
    EvalConfiguration,
    TrialDriverContractError,
    TrialRunResult,
    run_trials,
)
from loopforge.application.graders import GraderEvidence
from loopforge.domain.benchmarks import (
    BenchmarkCategory,
    BenchmarkReport,
    BenchmarkSandboxMode,
    BenchmarkTaskSpec,
    GraderId,
    suite_lock_hash,
)
from loopforge.domain.events import (
    ApprovalGranted,
    BudgetDebited,
    Event,
    ModelTurnRecorded,
    RunStarted,
    RunStopped,
    VerificationPassed,
)
from loopforge.domain.types import (
    ActionId,
    EventId,
    RunId,
    RunStatus,
    StopReason,
    UsageDelta,
)

_EPOCH = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return _EPOCH + timedelta(seconds=seconds)


class _EventIds:
    """Small deterministic event-id/run-id sequencer for canned streams."""

    def __init__(self, run_id: str) -> None:
        self.run_id = RunId(run_id)
        self.sequence = 0

    def next(self) -> tuple[EventId, RunId, int]:
        self.sequence += 1
        return EventId(f"evt-{self.run_id}-{self.sequence}"), self.run_id, self.sequence


def _started(ids: _EventIds, *, at: float = 0.0) -> RunStarted:
    event_id, run_id, sequence = ids.next()
    return RunStarted(
        event_id=event_id, run_id=run_id, occurred_at=_at(at), sequence=sequence, objective="obj"
    )


def _debit(ids: _EventIds, *, cost: float, tokens: int = 120, at: float) -> BudgetDebited:
    event_id, run_id, sequence = ids.next()
    return BudgetDebited(
        event_id=event_id,
        run_id=run_id,
        occurred_at=_at(at),
        sequence=sequence,
        usage=UsageDelta(cost_usd=cost, input_tokens=tokens - 20, output_tokens=20),
    )


def _turn_recorded(
    ids: _EventIds, *, provider: str = "stub", model: str = "cheap", at: float
) -> ModelTurnRecorded:
    event_id, run_id, sequence = ids.next()
    return ModelTurnRecorded(
        event_id=event_id,
        run_id=run_id,
        occurred_at=_at(at),
        sequence=sequence,
        provider=provider,
        model=model,
        action_id=ActionId(f"{run_id}-a{sequence}"),
    )


def _verified(ids: _EventIds, *, at: float) -> VerificationPassed:
    event_id, run_id, sequence = ids.next()
    return VerificationPassed(
        event_id=event_id,
        run_id=run_id,
        occurred_at=_at(at),
        sequence=sequence,
        summary="run_tests: passed",
    )


def _approval_granted(ids: _EventIds, *, at: float) -> ApprovalGranted:
    event_id, run_id, sequence = ids.next()
    return ApprovalGranted(
        event_id=event_id,
        run_id=run_id,
        occurred_at=_at(at),
        sequence=sequence,
        action_id=ActionId(f"{run_id}-approval"),
    )


def _stopped(ids: _EventIds, reason: StopReason, *, at: float, summary: str = "") -> RunStopped:
    event_id, run_id, sequence = ids.next()
    return RunStopped(
        event_id=event_id,
        run_id=run_id,
        occurred_at=_at(at),
        sequence=sequence,
        reason=reason,
        summary=summary or reason.value,
    )


def _success_events(run_id: str, *, cost: float, latency: float) -> tuple[Event, ...]:
    ids = _EventIds(run_id)
    return (
        _started(ids, at=0.0),
        _debit(ids, cost=cost, at=1.0),
        _turn_recorded(ids, at=1.5),
        _verified(ids, at=2.0),
        _stopped(ids, StopReason.SUCCESS_VERIFIED, at=latency),
    )


def _failure_events(run_id: str, *, cost: float, latency: float) -> tuple[Event, ...]:
    ids = _EventIds(run_id)
    return (
        _started(ids, at=0.0),
        _debit(ids, cost=cost, at=1.0),
        _stopped(
            ids,
            StopReason.FAILURE,
            at=latency,
            summary="MODEL_UNAVAILABLE: provider is unavailable (HTTP 503)",
        ),
    )


def _evidence(changed: tuple[str, ...] = ()) -> GraderEvidence:
    return GraderEvidence(
        final_changed_files=changed,
        test_files=(),
        expected_test_files=(),
        final_sources=(),
        verification_summaries=(),
    )


def _result(
    run_id: str,
    status: RunStatus,
    events: tuple[Event, ...],
    *,
    changed: tuple[str, ...] = (),
) -> TrialRunResult:
    return TrialRunResult(run_id=run_id, status=status, events=events, evidence=_evidence(changed))


def _spec(task_id: str, *, prefixes: tuple[str, ...] = ("src",)) -> BenchmarkTaskSpec:
    return BenchmarkTaskSpec(
        task_id=task_id,
        category=BenchmarkCategory.SIMPLE_BUG,
        objective=f"Repair {task_id}.",
        fixture_id=task_id,
        sandbox_mode=BenchmarkSandboxMode.CONTAINER,
        grader_ids=(GraderId.VERIFIED_SUCCESS, GraderId.SCOPE_DISCIPLINE),
        allowed_prefixes=prefixes,
    )


def _config(config_id: str, **overrides: object) -> EvalConfiguration:
    kwargs: dict[str, object] = {
        "config_id": config_id,
        "max_cost_usd": 1.0,
        "max_iterations": 8,
    }
    kwargs.update(overrides)
    return EvalConfiguration(**kwargs)  # pyright: ignore[reportArgumentType]


class _StubDriver:
    """Canned per-(config, task) outcomes; records the trial ids it is handed."""

    def __init__(self, outcomes: Mapping[tuple[str, str], TrialRunResult]) -> None:
        self._outcomes = outcomes
        self.trial_ids: list[str] = []

    def __call__(
        self, spec: BenchmarkTaskSpec, config: EvalConfiguration, trial_id: str
    ) -> TrialRunResult:
        self.trial_ids.append(trial_id)
        return self._outcomes[(config.config_id, spec.task_id)]


def _run(
    specs: tuple[BenchmarkTaskSpec, ...],
    configs: tuple[EvalConfiguration, ...],
    trials_per_task: int,
    driver: _StubDriver,
) -> BenchmarkReport:
    return run_trials(
        specs,
        configs,
        trials_per_task,
        driver,
        report_id="report-1",
        suite_version="1.0.0",
        lock_hash=suite_lock_hash(specs),
    )


# --- EvalConfiguration validation (allow + deny) -------------------------------


def test_eval_configuration_defaults_are_valid() -> None:
    config = _config("cfg-a")
    assert config.no_progress_limit == 3
    assert config.router_enabled is False
    assert config.verify_read_only_turns is False
    assert config.expensive_models == frozenset()


@pytest.mark.parametrize("bad_id", ["", " ", "x" * 129, "bad\nid"])
def test_eval_configuration_rejects_bad_config_id(bad_id: str) -> None:
    with pytest.raises(ValueError, match="config_id"):
        _config(bad_id)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_eval_configuration_rejects_bad_budget(bad: float) -> None:
    with pytest.raises(ValueError, match="max_cost_usd"):
        _config("cfg-a", max_cost_usd=bad)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_eval_configuration_rejects_bad_max_iterations(bad: object) -> None:
    with pytest.raises(ValueError, match="max_iterations"):
        _config("cfg-a", max_iterations=bad)


@pytest.mark.parametrize("bad", [0, -1, False])
def test_eval_configuration_rejects_bad_no_progress_limit(bad: object) -> None:
    with pytest.raises(ValueError, match="no_progress_limit"):
        _config("cfg-a", no_progress_limit=bad)


def test_eval_configuration_rejects_non_bool_router_flag() -> None:
    with pytest.raises(TypeError, match="router_enabled must be a bool"):
        _config("cfg-a", router_enabled="yes")


def test_eval_configuration_rejects_non_bool_verify_read_only_turns() -> None:
    with pytest.raises(TypeError, match="verify_read_only_turns must be a bool"):
        _config("cfg-a", verify_read_only_turns="yes")  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "bad_entries",
    [
        frozenset({("only-provider",)}),
        frozenset({("provider", "")}),
        frozenset({("provider", "model", "extra")}),
    ],
)
def test_eval_configuration_rejects_bad_expensive_model_entries(
    bad_entries: frozenset[tuple[str, ...]],
) -> None:
    with pytest.raises(ValueError, match="expensive_models entries"):
        _config("cfg-a", expensive_models=bad_entries)


# --- TrialRunResult validation -------------------------------------------------


def test_trial_run_result_rejects_empty_run_id() -> None:
    with pytest.raises(ValueError, match="run_id cannot be empty"):
        TrialRunResult(run_id=" ", status=RunStatus.SUCCEEDED, events=(), evidence=_evidence())


def test_trial_run_result_rejects_non_event_entries() -> None:
    with pytest.raises(TypeError, match="must be domain events"):
        TrialRunResult(
            run_id="run-1",
            status=RunStatus.SUCCEEDED,
            events=("not-an-event",),  # type: ignore[arg-type]
            evidence=_evidence(),
        )


def test_trial_run_result_rejects_non_status() -> None:
    with pytest.raises(TypeError, match="status must be a RunStatus"):
        TrialRunResult(
            run_id="run-1",
            status="succeeded",  # type: ignore[arg-type]
            events=(),
            evidence=_evidence(),
        )


def test_trial_run_result_rejects_non_evidence() -> None:
    with pytest.raises(TypeError, match="evidence must be a GraderEvidence"):
        TrialRunResult(
            run_id="run-1",
            status=RunStatus.SUCCEEDED,
            events=(),
            evidence="not-evidence",  # type: ignore[arg-type]
        )


def test_trial_run_result_rejects_non_accounting_entries() -> None:
    with pytest.raises(TypeError, match="must be ContextAccounting instances"):
        TrialRunResult(
            run_id="run-1",
            status=RunStatus.SUCCEEDED,
            events=(),
            evidence=_evidence(),
            context_accountings=("not-an-accounting",),  # type: ignore[arg-type]
        )


def test_empty_event_stream_yields_zero_latency_and_no_success() -> None:
    spec = _spec("task-a")
    config = _config("cfg-a")
    driver = _StubDriver({("cfg-a", "task-a"): _result("run-empty", RunStatus.CREATED, ())})
    report = _run((spec,), (config,), 1, driver)
    entry = report.config_reports[0]
    assert entry.mean_latency_seconds == 0.0
    assert entry.successes == 0
    assert entry.false_successes == 0


# --- Aggregation math ------------------------------------------------------------


def test_aggregation_counts_and_means_are_exact() -> None:
    spec = _spec("task-a")
    config = _config("cfg-a")
    driver = _StubDriver(
        {
            ("cfg-a", "task-a"): _result(
                "run-ok", RunStatus.SUCCEEDED, _success_events("run-ok", cost=0.02, latency=10.0)
            )
        }
    )
    # Two trials: same canned outcome each — successes=2, means over the pair.
    report = _run((spec,), (config,), 2, driver)

    assert len(report.config_reports) == 1
    entry = report.config_reports[0]
    assert entry.config_id == "cfg-a"
    assert entry.task_id == "task-a"
    assert entry.trials == 2
    assert entry.successes == 2
    assert entry.false_successes == 0
    assert entry.success_rate == 1.0
    assert entry.false_success_rate == 0.0
    assert entry.mean_cost_usd == 0.02
    assert entry.mean_latency_seconds == 10.0
    assert entry.mean_total_tokens == 120.0
    assert entry.mean_human_interventions == 0.0
    assert report.pareto_config_ids == ("cfg-a",)
    assert report.suite_version == "1.0.0"
    assert report.lock_hash == suite_lock_hash((spec,))


def test_mixed_success_and_failure_trials_average_over_all_trials() -> None:
    spec = _spec("task-a")
    config = _config("cfg-a")

    class MixedDriver:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(
            self, spec: BenchmarkTaskSpec, config: EvalConfiguration, trial_id: str
        ) -> TrialRunResult:
            del spec, config, trial_id
            self.calls += 1
            if self.calls == 1:
                return _result(
                    "run-ok",
                    RunStatus.SUCCEEDED,
                    _success_events("run-ok", cost=0.02, latency=10.0),
                )
            return _result(
                "run-bad", RunStatus.FAILED, _failure_events("run-bad", cost=0.01, latency=4.0)
            )

    driver = MixedDriver()
    report = run_trials(
        (spec,),
        (config,),
        2,
        driver,
        report_id="report-1",
        suite_version="1.0.0",
        lock_hash=suite_lock_hash((spec,)),
    )
    entry = report.config_reports[0]
    assert entry.trials == 2
    assert entry.successes == 1
    assert entry.false_successes == 0
    assert entry.success_rate == 0.5
    assert entry.mean_cost_usd == 0.015
    assert entry.mean_latency_seconds == 7.0
    assert entry.mean_total_tokens == 120.0


def test_forged_success_counts_as_false_success_not_success() -> None:
    """The forged case: verifier-granted success with out-of-scope changes."""
    spec = _spec("task-a", prefixes=("src",))
    config = _config("cfg-a")
    forged = _result(
        "run-forged",
        RunStatus.SUCCEEDED,
        _success_events("run-forged", cost=0.02, latency=5.0),
        changed=("evil.py",),
    )
    driver = _StubDriver({("cfg-a", "task-a"): forged})
    report = _run((spec,), (config,), 2, driver)

    entry = report.config_reports[0]
    assert entry.successes == 0
    assert entry.false_successes == 2
    assert entry.success_rate == 0.0
    assert entry.false_success_rate == 1.0


def test_model_failure_trial_is_neither_success_nor_false_success() -> None:
    spec = _spec("task-a")
    config = _config("cfg-a")
    driver = _StubDriver(
        {
            ("cfg-a", "task-a"): _result(
                "run-f", RunStatus.FAILED, _failure_events("run-f", cost=0.0, latency=3.0)
            )
        }
    )
    report = _run((spec,), (config,), 1, driver)
    entry = report.config_reports[0]
    assert (entry.successes, entry.false_successes) == (0, 0)


# --- Trajectory + intervention wiring --------------------------------------------


def test_expensive_model_set_and_human_interventions_flow_into_records() -> None:
    spec = _spec("task-a")
    config = _config("cfg-a", expensive_models=frozenset({("stub", "cheap")}))
    ids = _EventIds("run-hitl")
    events: tuple[Event, ...] = (
        _started(ids, at=0.0),
        _debit(ids, cost=0.02, at=1.0),
        _turn_recorded(ids, provider="stub", model="cheap", at=1.5),
        _approval_granted(ids, at=2.0),
        _verified(ids, at=3.0),
        _stopped(ids, StopReason.SUCCESS_VERIFIED, at=4.0),
    )
    driver = _StubDriver({("cfg-a", "task-a"): _result("run-hitl", RunStatus.SUCCEEDED, events)})
    report = _run((spec,), (config,), 1, driver)
    entry = report.config_reports[0]
    assert entry.mean_human_interventions == 1.0
    # The trajectory metric is graded per trial inside the runner; the pinned
    # effect visible here is the intervention mean above (the expensive-model
    # projection itself is pinned in M4's tests).


# --- Pareto frontier ----------------------------------------------------------------


def _outcome(cost: float, latency: float, *, success: bool = True) -> TrialRunResult:
    run_id = f"run-{cost}-{latency}-{success}"
    if success:
        return _result(
            run_id, RunStatus.SUCCEEDED, _success_events(run_id, cost=cost, latency=latency)
        )
    return _result(run_id, RunStatus.FAILED, _failure_events(run_id, cost=cost, latency=latency))


def test_pareto_excludes_dominated_config() -> None:
    spec = _spec("task-a")
    configs = (_config("cfg-strong"), _config("cfg-weak"))
    driver = _StubDriver(
        {
            ("cfg-strong", "task-a"): _outcome(0.01, 5.0),
            # Same success rate but strictly worse cost and latency: dominated.
            ("cfg-weak", "task-a"): _outcome(0.05, 9.0),
        }
    )
    report = _run((spec,), configs, 1, driver)
    assert report.pareto_config_ids == ("cfg-strong",)


def test_pareto_retains_identically_tied_configs() -> None:
    spec = _spec("task-a")
    configs = (_config("cfg-a"), _config("cfg-b"))
    driver = _StubDriver(
        {
            ("cfg-a", "task-a"): _outcome(0.02, 5.0),
            ("cfg-b", "task-a"): _outcome(0.02, 5.0),
        }
    )
    report = _run((spec,), configs, 2, driver)
    assert report.pareto_config_ids == ("cfg-a", "cfg-b")


def test_pareto_retains_tradeoff_configs() -> None:
    spec = _spec("task-a")
    configs = (_config("cfg-accurate"), _config("cfg-cheap"))
    driver = _StubDriver(
        {
            # Higher success, higher cost — neither dominates the other.
            ("cfg-accurate", "task-a"): _outcome(0.05, 5.0),
            ("cfg-cheap", "task-a"): _outcome(0.01, 5.0, success=False),
        }
    )
    report = _run((spec,), configs, 1, driver)
    assert report.pareto_config_ids == ("cfg-accurate", "cfg-cheap")


def test_pareto_uses_config_level_means_across_tasks() -> None:
    specs = (_spec("task-a"), _spec("task-b"))
    configs = (_config("cfg-balanced"), _config("cfg-lopsided"))
    driver = _StubDriver(
        {
            # cfg-balanced: 1.0 success on both tasks, cheap on both.
            ("cfg-balanced", "task-a"): _outcome(0.01, 4.0),
            ("cfg-balanced", "task-b"): _outcome(0.01, 4.0),
            # cfg-lopsided: fails one task, equally costly — dominated on success.
            ("cfg-lopsided", "task-a"): _outcome(0.01, 4.0),
            ("cfg-lopsided", "task-b"): _outcome(0.01, 4.0, success=False),
        }
    )
    report = _run(specs, configs, 1, driver)
    # Deterministic ordering: sorted by (config_id, task_id).
    assert [(entry.config_id, entry.task_id) for entry in report.config_reports] == [
        ("cfg-balanced", "task-a"),
        ("cfg-balanced", "task-b"),
        ("cfg-lopsided", "task-a"),
        ("cfg-lopsided", "task-b"),
    ]
    assert report.pareto_config_ids == ("cfg-balanced",)


# --- Determinism ----------------------------------------------------------------------


def test_run_trials_guarantees_uniform_task_coverage_across_configs() -> None:
    # Pareto dominance vectors are per-config means over task reports, so
    # they are only commensurable when every config covers the same tasks;
    # run_trials iterates the full config x task cross product and this pins
    # that guarantee explicitly.
    specs = (_spec("task-a"), _spec("task-b"))
    configs = (_config("cfg-a"), _config("cfg-b"))
    outcomes = {
        (config_id, task_id): _outcome(0.01, 1.0)
        for config_id in ("cfg-a", "cfg-b")
        for task_id in ("task-a", "task-b")
    }
    driver = _StubDriver(outcomes)
    report = _run(specs, configs, 1, driver)
    coverage: dict[str, set[str]] = {}
    for entry in report.config_reports:
        coverage.setdefault(entry.config_id, set()).add(entry.task_id)
    assert coverage == {
        "cfg-a": {"task-a", "task-b"},
        "cfg-b": {"task-a", "task-b"},
    }


def test_trial_ids_and_driver_call_order_are_deterministic() -> None:
    specs = (_spec("task-b"), _spec("task-a"))  # deliberately unsorted
    configs = (_config("cfg-b"), _config("cfg-a"))  # deliberately unsorted
    outcomes = {
        (config_id, task_id): _outcome(0.01, 1.0)
        for config_id in ("cfg-a", "cfg-b")
        for task_id in ("task-a", "task-b")
    }
    driver = _StubDriver(outcomes)
    _run(specs, configs, 2, driver)
    assert driver.trial_ids == [
        "cfg-a:task-a:0",
        "cfg-a:task-a:1",
        "cfg-a:task-b:0",
        "cfg-a:task-b:1",
        "cfg-b:task-a:0",
        "cfg-b:task-a:1",
        "cfg-b:task-b:0",
        "cfg-b:task-b:1",
    ]


# --- Latency derivation -----------------------------------------------------------------


def test_terminal_stream_latency_is_started_to_stopped() -> None:
    spec = _spec("task-a")
    config = _config("cfg-a")
    driver = _StubDriver({("cfg-a", "task-a"): _outcome(0.01, 12.5)})
    report = _run((spec,), (config,), 1, driver)
    assert report.config_reports[0].mean_latency_seconds == 12.5


def test_non_terminal_stream_latency_is_first_to_last_event() -> None:
    """A wedged stream has no trailing RunStopped; graders already mark it not-success."""
    spec = _spec("task-a")
    config = _config("cfg-a")
    ids = _EventIds("run-wedged")
    wedged: tuple[Event, ...] = (
        _started(ids, at=10.0),
        _debit(ids, cost=0.01, at=15.0),
        _turn_recorded(ids, at=18.0),
    )
    driver = _StubDriver({("cfg-a", "task-a"): _result("run-wedged", RunStatus.READY, wedged)})
    report = _run((spec,), (config,), 1, driver)
    entry = report.config_reports[0]
    assert entry.mean_latency_seconds == 8.0
    assert entry.successes == 0
    assert entry.false_successes == 0


# --- Runner validation (allow + deny) ------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, True, 2.5])
def test_run_trials_rejects_bad_trials_per_task(bad: object) -> None:
    spec = _spec("task-a")
    driver = _StubDriver({("cfg-a", "task-a"): _outcome(0.01, 1.0)})
    with pytest.raises(ValueError, match="trials_per_task"):
        _run((spec,), (_config("cfg-a"),), bad, driver)  # type: ignore[arg-type]


def test_run_trials_rejects_empty_specs() -> None:
    driver = _StubDriver({})
    with pytest.raises(ValueError, match="specs cannot be empty"):
        run_trials(
            (),
            (_config("cfg-a"),),
            1,
            driver,
            report_id="report-1",
            suite_version="1.0.0",
            lock_hash="0" * 64,
        )


def test_run_trials_rejects_non_spec_entries() -> None:
    driver = _StubDriver({})
    with pytest.raises(TypeError, match="must be BenchmarkTaskSpec instances"):
        run_trials(
            ("not-a-spec",),  # type: ignore[arg-type]
            (_config("cfg-a"),),
            1,
            driver,
            report_id="report-1",
            suite_version="1.0.0",
            lock_hash="0" * 64,
        )


def test_run_trials_rejects_non_config_entries() -> None:
    spec = _spec("task-a")
    driver = _StubDriver({("cfg-a", "task-a"): _outcome(0.01, 1.0)})
    with pytest.raises(TypeError, match="must be EvalConfiguration instances"):
        run_trials(
            (spec,),
            ("not-a-config",),  # type: ignore[arg-type]
            1,
            driver,
            report_id="report-1",
            suite_version="1.0.0",
            lock_hash=suite_lock_hash((spec,)),
        )


def test_run_trials_rejects_duplicate_task_ids() -> None:
    specs = (_spec("task-a"), _spec("task-a"))
    driver = _StubDriver({("cfg-a", "task-a"): _outcome(0.01, 1.0)})
    with pytest.raises(ValueError, match="task_ids must be unique"):
        _run(specs, (_config("cfg-a"),), 1, driver)


def test_run_trials_rejects_empty_configs() -> None:
    driver = _StubDriver({})
    with pytest.raises(ValueError, match="configurations cannot be empty"):
        _run((_spec("task-a"),), (), 1, driver)


def test_run_trials_rejects_duplicate_config_ids() -> None:
    spec = _spec("task-a")
    configs = (_config("cfg-a"), _config("cfg-a"))
    driver = _StubDriver({("cfg-a", "task-a"): _outcome(0.01, 1.0)})
    with pytest.raises(ValueError, match="config_ids must be unique"):
        _run((spec,), configs, 1, driver)


def test_run_trials_rejects_driver_contract_violation() -> None:
    class BadDriver:
        def __call__(
            self, spec: BenchmarkTaskSpec, config: EvalConfiguration, trial_id: str
        ) -> str:
            del spec, config, trial_id
            return "not-a-trial-result"

    spec = _spec("task-a")
    with pytest.raises(TrialDriverContractError, match="expected TrialRunResult"):
        run_trials(
            (spec,),
            (_config("cfg-a"),),
            1,
            BadDriver(),  # type: ignore[arg-type]
            report_id="report-1",
            suite_version="1.0.0",
            lock_hash=suite_lock_hash((spec,)),
        )


def test_run_trials_rejects_events_from_a_foreign_run() -> None:
    # A driver that mixes another run's events into the trial result would
    # silently grade and meter foreign work as this trial's; the runner
    # refuses the mismatched stream as a contract violation.
    spec = _spec("task-a")
    foreign = _result(
        "run-claimed",
        RunStatus.SUCCEEDED,
        _success_events("run-foreign", cost=0.02, latency=5.0),
    )
    driver = _StubDriver({("cfg-a", "task-a"): foreign})
    with pytest.raises(TrialDriverContractError, match="does not match the trial run_id"):
        _run((spec,), (_config("cfg-a"),), 1, driver)


def test_run_trials_echoes_but_never_forges_the_lock() -> None:
    """The domain report refuses a malformed hash; the runner passes it through verbatim."""
    spec = _spec("task-a")
    driver = _StubDriver({("cfg-a", "task-a"): _outcome(0.01, 1.0)})
    with pytest.raises(ValueError, match="report lock_hash"):
        run_trials(
            (spec,),
            (_config("cfg-a"),),
            1,
            driver,
            report_id="report-1",
            suite_version="1.0.0",
            lock_hash="not-hex",
        )


# --- PACS-017 M5: policy references, context/recovery axes -----------------------

from loopforge.domain.events import RetryScheduled  # noqa: E402


def _retry_scheduled(ids: _EventIds, *, at: float) -> RetryScheduled:
    event_id, run_id, sequence = ids.next()
    return RetryScheduled(
        event_id=event_id,
        run_id=run_id,
        occurred_at=_at(at),
        sequence=sequence,
        action_id=ActionId(f"{run_id}-a{sequence}"),
        next_attempt=2,
        delay_seconds=0.5,
        reason_code="RETRY_TRANSIENT_FAILURE",
    )


def _context_outcome(
    run_id: str,
    *,
    cost: float = 0.02,
    latency: float = 5.0,
    tokens: int = 120,
    retries: int = 0,
) -> TrialRunResult:
    """Canned success whose context footprint and recovery count are pinned."""
    ids = _EventIds(run_id)
    events: list[Event] = [
        _started(ids, at=0.0),
        _debit(ids, cost=cost, tokens=tokens, at=1.0),
    ]
    events.extend(_retry_scheduled(ids, at=1.2) for _ in range(retries))
    events.extend(
        [
            _turn_recorded(ids, at=1.5),
            _verified(ids, at=2.0),
            _stopped(ids, StopReason.SUCCESS_VERIFIED, at=latency),
        ]
    )
    return _result(run_id, RunStatus.SUCCEEDED, tuple(events))


def test_eval_configuration_accepts_a_versioned_policy_reference() -> None:
    config = _config("cfg-policy", policy_id="adaptive-context", policy_version=1)
    assert config.policy_id == "adaptive-context"
    assert config.policy_version == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"policy_id": "adaptive-context"},
        {"policy_version": 1},
        {"policy_id": "bad id!", "policy_version": 1},
        {"policy_id": "x" * 65, "policy_version": 1},
        {"policy_id": "ok", "policy_version": 0},
        {"policy_id": "ok", "policy_version": -1},
        {"policy_id": "ok", "policy_version": True},
    ],
)
def test_eval_configuration_rejects_bad_policy_references(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="policy_"):
        _config("cfg-x", **overrides)


def test_aggregate_includes_context_efficiency_and_recovery_means() -> None:
    spec = _spec("task-a")
    configs = (_config("cfg-a"),)
    driver = _StubDriver({("cfg-a", "task-a"): _context_outcome("run-a", tokens=320, retries=2)})
    report = _run((spec,), configs, 2, driver)
    (entry,) = report.config_reports
    # _debit(tokens=320) debits 300 input tokens: the context peak per trial.
    assert entry.mean_context_tokens_used == 300.0
    assert entry.mean_recovery_events == 2.0
    assert entry.mean_context_items_dropped == 0.0


def test_pareto_dominance_counts_context_efficiency() -> None:
    spec = _spec("task-a")
    configs = (_config("cfg-lean"), _config("cfg-wide"))
    driver = _StubDriver(
        {
            # Identical legacy axes; cfg-lean assembles smaller contexts.
            ("cfg-lean", "task-a"): _context_outcome("run-lean", tokens=120),
            ("cfg-wide", "task-a"): _context_outcome("run-wide", tokens=420),
        }
    )
    report = _run((spec,), configs, 1, driver)
    assert report.pareto_config_ids == ("cfg-lean",)


def test_pareto_dominance_counts_recovery_events() -> None:
    spec = _spec("task-a")
    configs = (_config("cfg-steady"), _config("cfg-flailing"))
    driver = _StubDriver(
        {
            # Identical legacy and context axes; cfg-steady recovers less.
            ("cfg-steady", "task-a"): _context_outcome("run-steady"),
            ("cfg-flailing", "task-a"): _context_outcome("run-flailing", retries=2),
        }
    )
    report = _run((spec,), configs, 1, driver)
    assert report.pareto_config_ids == ("cfg-steady",)


def test_pareto_retains_context_recovery_tradeoffs() -> None:
    spec = _spec("task-a")
    configs = (_config("cfg-lean-flailing"), _config("cfg-wide-steady"))
    driver = _StubDriver(
        {
            ("cfg-lean-flailing", "task-a"): _context_outcome("run-lf", retries=2),
            ("cfg-wide-steady", "task-a"): _context_outcome("run-ws", tokens=420),
        }
    )
    report = _run((spec,), configs, 1, driver)
    assert report.pareto_config_ids == ("cfg-lean-flailing", "cfg-wide-steady")


def test_candidate_vs_active_laboratory_comparison_is_deterministic() -> None:
    """The M5 A/B contract: active config vs policy-carrying candidate config,
    same locked suite, same scripted outcomes, byte-identical reports."""
    spec = _spec("task-a")
    configs = (
        _config("baseline"),
        _config("policy-adaptive-context", policy_id="adaptive-context", policy_version=1),
    )
    outcomes = {
        ("baseline", "task-a"): _context_outcome("run-active", tokens=420),
        ("policy-adaptive-context", "task-a"): _context_outcome("run-candidate"),
    }
    report = _run((spec,), configs, 2, _StubDriver(outcomes))
    again = _run((spec,), configs, 2, _StubDriver(outcomes))
    assert report == again
    assert {entry.config_id for entry in report.config_reports} == {
        "baseline",
        "policy-adaptive-context",
    }
    # Same success/cost/latency; the candidate's leaner contexts dominate.
    assert report.pareto_config_ids == ("policy-adaptive-context",)
