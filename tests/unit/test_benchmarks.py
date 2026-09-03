"""Unit tests for the locked benchmark / multi-trial evaluation vocabulary (PACS-016 M1).

Every validation rule is pinned with an allow+deny pair (AGENTS.md rule 10);
the suite lock hash is pinned to a literal sha256 for a fixed task tuple so
any canonicalization change is loud, and pinned order-insensitive to task
construction order.
"""

from __future__ import annotations

import pytest

from loopforge.domain.benchmarks import (
    BenchmarkCategory,
    BenchmarkReport,
    BenchmarkSandboxMode,
    BenchmarkSuite,
    BenchmarkTaskSpec,
    ConfigReport,
    GraderId,
    GraderResult,
    GraderVerdict,
    TrajectoryMetrics,
    TrialRecord,
    suite_lock_hash,
)
from loopforge.domain.types import RunStatus

# Fixed example suite for the lock-hash determinism pin: any change to the
# canonical serialization changes this literal and fails loudly.
_PINNED_LOCK_HASH = "ae63a152f9611ae2247b298218b02022a8d5ef11df9660fe11800cb9c1888ca6"


def _pinned_tasks() -> tuple[BenchmarkTaskSpec, ...]:
    return (
        BenchmarkTaskSpec(
            task_id="task-alpha",
            category=BenchmarkCategory.SIMPLE_BUG,
            objective="Fix the off-by-one error in the iterator.",
            fixture_id="fixture-simple-bug",
            sandbox_mode=BenchmarkSandboxMode.TRUSTED_LOCAL,
            grader_ids=(GraderId.VERIFIED_SUCCESS, GraderId.SCOPE_DISCIPLINE),
            allowed_prefixes=("src/calc",),
        ),
        BenchmarkTaskSpec(
            task_id="task-beta",
            category=BenchmarkCategory.PROMPT_INJECTION,
            objective="Repair the parser despite the injected README instruction.",
            fixture_id="fixture-injection",
            sandbox_mode=BenchmarkSandboxMode.CONTAINER,
            grader_ids=(GraderId.GROUND_TRUTH,),
            live_eligible=True,
        ),
    )


def _task(**overrides: object) -> BenchmarkTaskSpec:
    kwargs: dict[str, object] = {
        "task_id": "task-1",
        "category": BenchmarkCategory.SIMPLE_BUG,
        "objective": "Fix the off-by-one error in the iterator.",
        "fixture_id": "fixture-simple-bug",
        "sandbox_mode": BenchmarkSandboxMode.TRUSTED_LOCAL,
        "grader_ids": (GraderId.VERIFIED_SUCCESS,),
    }
    kwargs.update(overrides)
    return BenchmarkTaskSpec(**kwargs)  # pyright: ignore[reportArgumentType]


def _metrics(**overrides: object) -> TrajectoryMetrics:
    kwargs: dict[str, object] = {
        "model_turns": 12,
        "repetition_ratio": 0.25,
        "expensive_model_turns": 3,
        "scope_violations": 0,
        "permission_requests": 2,
        "context_tokens_used": 40_000,
        "context_items_dropped": 1,
        "recovery_events": 1,
    }
    kwargs.update(overrides)
    return TrajectoryMetrics(**kwargs)  # pyright: ignore[reportArgumentType]


def _grader_result(**overrides: object) -> GraderResult:
    kwargs: dict[str, object] = {
        "grader_id": GraderId.VERIFIED_SUCCESS,
        "verdict": GraderVerdict.PASS,
        "detail": "all required commands passed",
    }
    kwargs.update(overrides)
    return GraderResult(**kwargs)  # pyright: ignore[reportArgumentType]


def _trial(**overrides: object) -> TrialRecord:
    kwargs: dict[str, object] = {
        "trial_id": "trial-1",
        "task_id": "task-1",
        "config_id": "config-a",
        "run_id": "run-1",
        "status": RunStatus.SUCCEEDED,
        "metrics": _metrics(),
        "grader_results": (_grader_result(),),
        "cost_usd": 0.42,
        "total_tokens": 12_345,
        "latency_seconds": 61.5,
        "human_interventions": 1,
    }
    kwargs.update(overrides)
    return TrialRecord(**kwargs)  # pyright: ignore[reportArgumentType]


def _config_report(**overrides: object) -> ConfigReport:
    kwargs: dict[str, object] = {
        "config_id": "config-a",
        "task_id": "task-1",
        "trials": 4,
        "successes": 3,
        "false_successes": 1,
        "success_rate": 0.75,
        "false_success_rate": 0.25,
        "mean_cost_usd": 0.5,
        "mean_latency_seconds": 60.0,
        "mean_total_tokens": 10_000.0,
        "mean_human_interventions": 0.5,
    }
    kwargs.update(overrides)
    return ConfigReport(**kwargs)  # pyright: ignore[reportArgumentType]


def _suite(
    tasks: tuple[BenchmarkTaskSpec, ...] | None = None, **overrides: object
) -> BenchmarkSuite:
    suite_tasks = tasks if tasks is not None else (_task(),)
    kwargs: dict[str, object] = {
        "version": "1.0.0",
        "tasks": suite_tasks,
        "lock_hash": suite_lock_hash(suite_tasks),
    }
    kwargs.update(overrides)
    return BenchmarkSuite(**kwargs)  # pyright: ignore[reportArgumentType]


def _report(**overrides: object) -> BenchmarkReport:
    kwargs: dict[str, object] = {
        "report_id": "report-1",
        "suite_version": "1.0.0",
        "lock_hash": suite_lock_hash((_task(),)),
        "config_reports": (_config_report(),),
        "pareto_config_ids": ("config-a",),
    }
    kwargs.update(overrides)
    return BenchmarkReport(**kwargs)  # pyright: ignore[reportArgumentType]


def test_benchmark_category_vocabulary_is_closed_at_twelve() -> None:
    assert len(BenchmarkCategory) == 12
    assert {member.name for member in BenchmarkCategory} == {
        "SIMPLE_BUG",
        "MULTI_FILE",
        "MISLEADING_FAILURE",
        "TRANSIENT_API",
        "AMBIGUOUS_SUCCESS",
        "CONTEXT_POLLUTION",
        "STALE_STATE",
        "STALL",
        "PROMPT_INJECTION",
        "HITL",
        "PARALLEL_WORK",
        "PROVIDER_OUTAGE",
    }


def test_benchmark_sandbox_mode_vocabulary_is_closed_at_two() -> None:
    assert {member.value for member in BenchmarkSandboxMode} == {
        "trusted_local",
        "container",
    }


def test_grader_id_vocabulary_is_closed_at_four() -> None:
    assert {member.name for member in GraderId} == {
        "VERIFIED_SUCCESS",
        "SCOPE_DISCIPLINE",
        "GROUND_TRUTH",
        "RECOVERY",
    }


def test_grader_verdict_vocabulary_has_first_class_false_success() -> None:
    assert {member.name for member in GraderVerdict} == {"PASS", "FAIL", "FALSE_SUCCESS"}


def test_grader_result_accepts_bounded_detail() -> None:
    result = _grader_result(detail="x" * 300)
    assert result.verdict is GraderVerdict.PASS


def test_grader_result_rejects_non_enum_grader_id() -> None:
    with pytest.raises(TypeError, match="grader_id must be a GraderId"):
        _grader_result(grader_id="verified_success")


def test_grader_result_rejects_non_enum_verdict() -> None:
    with pytest.raises(TypeError, match="grader verdict must be a GraderVerdict"):
        _grader_result(verdict="pass")


def test_grader_result_rejects_non_string_detail() -> None:
    with pytest.raises(TypeError, match="grader detail must be a string"):
        _grader_result(detail=42)


def test_grader_result_rejects_overlong_detail() -> None:
    with pytest.raises(ValueError, match="grader detail cannot exceed 300 characters"):
        _grader_result(detail="x" * 301)


def test_grader_result_rejects_control_characters_in_detail() -> None:
    with pytest.raises(ValueError, match="grader detail must not contain control characters"):
        _grader_result(detail="bad\x07detail")


def test_task_spec_accepts_minimal_construction() -> None:
    task = _task()
    assert task.allowed_prefixes == ()
    assert task.live_eligible is False


def test_task_spec_accepts_valid_allowed_prefixes() -> None:
    task = _task(allowed_prefixes=("src/calc", "tests/unit/test_calc.py"))
    assert task.allowed_prefixes == ("src/calc", "tests/unit/test_calc.py")


@pytest.mark.parametrize("field", ["task_id", "objective", "fixture_id"])
def test_task_spec_rejects_empty_required_strings(field: str) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        _task(**{field: "  "})


@pytest.mark.parametrize("bad_id", ["x" * 129, "bad\x07id"])
def test_task_spec_rejects_overlong_or_control_character_ids(bad_id: str) -> None:
    with pytest.raises(ValueError, match="must not contain control characters or exceed 128"):
        _task(task_id=bad_id)


def test_task_spec_rejects_non_enum_category() -> None:
    with pytest.raises(TypeError, match="category must be a BenchmarkCategory"):
        _task(category="simple_bug")


def test_task_spec_rejects_non_enum_sandbox_mode() -> None:
    with pytest.raises(TypeError, match="sandbox mode must be a BenchmarkSandboxMode"):
        _task(sandbox_mode="container")


def test_task_spec_rejects_overlong_objective() -> None:
    with pytest.raises(ValueError, match="objective cannot exceed 2000 characters"):
        _task(objective="x" * 2001)


def test_task_spec_rejects_empty_grader_ids() -> None:
    with pytest.raises(ValueError, match="at least one grader"):
        _task(grader_ids=())


def test_task_spec_rejects_non_enum_grader_ids() -> None:
    with pytest.raises(TypeError, match="grader_ids must be GraderId members"):
        _task(grader_ids=(GraderId.VERIFIED_SUCCESS, "ground_truth"))


def test_task_spec_rejects_duplicate_grader_ids() -> None:
    with pytest.raises(ValueError, match="grader_ids must be unique"):
        _task(grader_ids=(GraderId.RECOVERY, GraderId.RECOVERY))


@pytest.mark.parametrize(
    "prefix",
    ["/abs/path", "src/../secret", "src/./x", ".git/hooks", "a//b", "back\\slash", ""],
)
def test_task_spec_rejects_unsafe_allowed_prefixes(prefix: str) -> None:
    with pytest.raises(ValueError, match="allowed path prefix"):
        _task(allowed_prefixes=(prefix,))


def test_task_spec_rejects_non_string_prefix() -> None:
    with pytest.raises(TypeError, match="allowed path prefixes must be strings"):
        _task(allowed_prefixes=(42,))


def test_task_spec_rejects_non_bool_live_eligible() -> None:
    with pytest.raises(TypeError, match="live_eligible must be a bool"):
        _task(live_eligible=1)


def test_trajectory_metrics_defaults_are_zero() -> None:
    metrics = TrajectoryMetrics()
    assert metrics.model_turns == 0
    assert metrics.repetition_ratio == 0.0
    assert metrics.recovery_events == 0


def test_trajectory_metrics_accepts_full_construction() -> None:
    metrics = _metrics()
    assert metrics.expensive_model_turns == 3
    assert metrics.context_tokens_used == 40_000


@pytest.mark.parametrize(
    "field",
    [
        "model_turns",
        "expensive_model_turns",
        "scope_violations",
        "permission_requests",
        "context_tokens_used",
        "context_items_dropped",
        "recovery_events",
    ],
)
def test_trajectory_metrics_rejects_negative_ints(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} cannot be negative"):
        _metrics(**{field: -1})


@pytest.mark.parametrize("value", [True, 1.5])
def test_trajectory_metrics_rejects_non_int_counts(value: object) -> None:
    with pytest.raises(ValueError, match="model_turns must be an integer"):
        _metrics(model_turns=value)


@pytest.mark.parametrize("ratio", [-0.1, 1.1, float("nan"), float("inf")])
def test_trajectory_metrics_rejects_out_of_range_repetition_ratio(ratio: float) -> None:
    with pytest.raises(ValueError, match="repetition_ratio"):
        _metrics(repetition_ratio=ratio)


def test_trial_record_accepts_full_construction() -> None:
    trial = _trial()
    assert trial.status is RunStatus.SUCCEEDED
    assert len(trial.grader_results) == 1


@pytest.mark.parametrize("field", ["trial_id", "task_id", "config_id", "run_id"])
def test_trial_record_rejects_empty_ids(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} cannot be empty"):
        _trial(**{field: ""})


def test_trial_record_rejects_non_enum_status() -> None:
    with pytest.raises(TypeError, match="trial status must be a RunStatus"):
        _trial(status="succeeded")


def test_trial_record_rejects_non_metrics_object() -> None:
    with pytest.raises(TypeError, match="trial metrics must be a TrajectoryMetrics"):
        _trial(metrics={"model_turns": 3})


def test_trial_record_rejects_non_grader_result_entries() -> None:
    with pytest.raises(TypeError, match="grader results must be GraderResult instances"):
        _trial(grader_results=("not-a-result",))


def test_trial_record_rejects_duplicate_grader_result_ids() -> None:
    duplicate = (
        _grader_result(),
        _grader_result(verdict=GraderVerdict.FALSE_SUCCESS),
    )
    with pytest.raises(ValueError, match="unique grader_ids"):
        _trial(grader_results=duplicate)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01])
def test_trial_record_rejects_bad_cost(bad: float) -> None:
    with pytest.raises(ValueError, match="trial cost_usd"):
        _trial(cost_usd=bad)


def test_trial_record_rejects_negative_total_tokens() -> None:
    with pytest.raises(ValueError, match="trial total_tokens cannot be negative"):
        _trial(total_tokens=-1)


@pytest.mark.parametrize("bad", [float("nan"), -1.0])
def test_trial_record_rejects_bad_latency(bad: float) -> None:
    with pytest.raises(ValueError, match="trial latency_seconds"):
        _trial(latency_seconds=bad)


def test_trial_record_rejects_negative_human_interventions() -> None:
    with pytest.raises(ValueError, match="trial human_interventions cannot be negative"):
        _trial(human_interventions=-1)


def test_config_report_accepts_consistent_counts_and_rates() -> None:
    report = _config_report()
    assert report.success_rate == 0.75
    assert report.false_success_rate == 0.25


@pytest.mark.parametrize("trials", [0, -3])
def test_config_report_rejects_non_positive_trials(trials: int) -> None:
    with pytest.raises(ValueError, match="trials must be positive"):
        _config_report(trials=trials)


def test_config_report_rejects_non_int_trials() -> None:
    with pytest.raises(ValueError, match="trials must be an integer"):
        _config_report(trials=True)


def test_config_report_rejects_negative_successes() -> None:
    with pytest.raises(ValueError, match="successes cannot be negative"):
        _config_report(successes=-1, success_rate=0.0)


def test_config_report_rejects_successes_above_trials() -> None:
    with pytest.raises(ValueError, match="successes cannot exceed trials"):
        _config_report(successes=5, success_rate=1.0)


def test_config_report_rejects_false_successes_above_trials() -> None:
    with pytest.raises(ValueError, match="false_successes cannot exceed trials"):
        _config_report(false_successes=5, false_success_rate=1.0)


@pytest.mark.parametrize("rate", [-0.1, 1.1, float("nan")])
def test_config_report_rejects_out_of_range_success_rate(rate: float) -> None:
    with pytest.raises(ValueError, match="success_rate"):
        _config_report(success_rate=rate)


def test_config_report_rejects_success_rate_inconsistent_with_counts() -> None:
    with pytest.raises(ValueError, match="success_rate must equal successes / trials"):
        _config_report(success_rate=0.5)


def test_config_report_rejects_false_success_rate_inconsistent_with_counts() -> None:
    with pytest.raises(ValueError, match="false_success_rate must equal false_successes / trials"):
        _config_report(false_success_rate=0.0)


@pytest.mark.parametrize(
    "field",
    [
        "mean_cost_usd",
        "mean_latency_seconds",
        "mean_total_tokens",
        "mean_human_interventions",
    ],
)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_config_report_rejects_bad_means(field: str, bad: float) -> None:
    with pytest.raises(ValueError, match=field):
        _config_report(**{field: bad})


def test_benchmark_report_accepts_pareto_subset_of_configs() -> None:
    configs = (
        _config_report(config_id="config-a"),
        _config_report(config_id="config-b"),
    )
    report = _report(config_reports=configs, pareto_config_ids=("config-b",))
    assert report.pareto_config_ids == ("config-b",)


def test_benchmark_report_rejects_empty_report_id() -> None:
    with pytest.raises(ValueError, match="report_id cannot be empty"):
        _report(report_id=" ")


def test_benchmark_report_rejects_empty_suite_version() -> None:
    with pytest.raises(ValueError, match="suite_version cannot be empty"):
        _report(suite_version="")


@pytest.mark.parametrize("bad_hash", ["", "zz" + "0" * 62])
def test_benchmark_report_rejects_malformed_lock_hash(bad_hash: str) -> None:
    with pytest.raises(ValueError, match="report lock_hash"):
        _report(lock_hash=bad_hash)


def test_benchmark_report_rejects_empty_config_reports() -> None:
    with pytest.raises(ValueError, match="at least one config report"):
        _report(config_reports=(), pareto_config_ids=())


def test_benchmark_report_rejects_non_config_report_entries() -> None:
    with pytest.raises(TypeError, match="must be ConfigReport instances"):
        _report(config_reports=("not-a-report",))


def test_benchmark_report_rejects_duplicate_config_ids() -> None:
    with pytest.raises(ValueError, match="config_ids must be unique"):
        _report(config_reports=(_config_report(), _config_report()))


def test_benchmark_report_rejects_duplicate_pareto_ids() -> None:
    with pytest.raises(ValueError, match="pareto_config_ids must be unique"):
        _report(pareto_config_ids=("config-a", "config-a"))


def test_benchmark_report_rejects_pareto_referencing_unknown_config() -> None:
    with pytest.raises(ValueError, match="pareto config 'config-zzz' is not part of the report"):
        _report(pareto_config_ids=("config-zzz",))


def test_suite_lock_hash_is_pinned_deterministic() -> None:
    assert suite_lock_hash(_pinned_tasks()) == _PINNED_LOCK_HASH


def test_suite_lock_hash_is_insensitive_to_task_order() -> None:
    tasks = _pinned_tasks()
    assert suite_lock_hash(tuple(reversed(tasks))) == _PINNED_LOCK_HASH


def test_suite_lock_hash_changes_on_semantic_edit() -> None:
    edited = (
        _pinned_tasks()[0],
        BenchmarkTaskSpec(
            task_id="task-beta",
            category=BenchmarkCategory.PROMPT_INJECTION,
            objective="A different objective.",
            fixture_id="fixture-injection",
            sandbox_mode=BenchmarkSandboxMode.CONTAINER,
            grader_ids=(GraderId.GROUND_TRUTH,),
            live_eligible=True,
        ),
    )
    assert suite_lock_hash(edited) != _PINNED_LOCK_HASH


def test_suite_lock_hash_rejects_empty_suite() -> None:
    with pytest.raises(ValueError, match="at least one task"):
        suite_lock_hash(())


def test_suite_lock_hash_rejects_non_task_entries() -> None:
    with pytest.raises(TypeError, match="must be BenchmarkTaskSpec instances"):
        suite_lock_hash(("not-a-task",))  # type: ignore[arg-type]


def test_suite_lock_hash_rejects_duplicate_task_ids() -> None:
    with pytest.raises(ValueError, match="task_ids must be unique"):
        suite_lock_hash((_task(), _task()))


def test_benchmark_suite_locks_to_its_content() -> None:
    suite = _suite(tasks=_pinned_tasks())
    assert suite.lock_hash == _PINNED_LOCK_HASH


def test_benchmark_suite_rejects_empty_version() -> None:
    with pytest.raises(ValueError, match="suite version cannot be empty"):
        _suite(version="")


def test_benchmark_suite_rejects_empty_tasks() -> None:
    with pytest.raises(ValueError, match="at least one task"):
        BenchmarkSuite(version="1.0.0", tasks=(), lock_hash="ab" * 32)


def test_benchmark_suite_rejects_malformed_lock_hash() -> None:
    with pytest.raises(ValueError, match="suite lock_hash"):
        _suite(lock_hash="not-hex")


def test_benchmark_suite_rejects_lock_hash_mismatch() -> None:
    other_hash = suite_lock_hash((_task(task_id="task-other"),))
    with pytest.raises(ValueError, match="does not match the locked task content"):
        _suite(lock_hash=other_hash)


def test_benchmark_suite_rejects_duplicate_task_ids() -> None:
    with pytest.raises(ValueError, match="task_ids must be unique"):
        BenchmarkSuite(version="1.0.0", tasks=(_task(), _task()), lock_hash="ab" * 32)
