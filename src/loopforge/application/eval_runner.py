"""Multi-trial benchmark evaluation runner and report aggregation (PACS-016, M5).

The runner executes every (configuration, task) pair for ``trials_per_task``
trials through a caller-supplied ``TrialDriver``, grades each trial with the
M3 deterministic graders, projects the M4 trajectory metrics and human
intervention counts, and aggregates everything into the M1
``BenchmarkReport`` — including the Pareto frontier over configurations.

Layer boundary (lint-imports): this module sits in ``application`` and
imports domain + ports + sibling application modules only. It NEVER sees
``BenchmarkTaskBinding`` (workloads) or any adapter: the M6 entrypoints
driver adapts locked bindings to the ``TrialDriver`` seam — building and
closing runtime bundles, injecting faults per the binding's descriptor, and
collecting the operator-owned ``GraderEvidence`` from the end-of-run
workspace. The runner's inputs are exactly the M1 domain spec, the M3
evidence carrier, and the durable event stream.

Authority boundaries: benchmark content (specs, lock hash, suite version) is
operator-owned and only ever ECHOED into the report — the runner never
recomputes or alters it. ``EvalConfiguration`` is data-only and may only
narrow or select within authority the operator already granted (AGENTS.md
rules 11, 12); it carries no permissions, sandbox capabilities, or stopping
authority of its own.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from loopforge.application.graders import (
    GraderEvidence,
    grade_trial,
    trial_is_false_success,
    trial_is_success,
)
from loopforge.application.trajectory import (
    compute_trajectory_metrics,
    count_human_interventions,
)
from loopforge.domain.benchmarks import (
    BenchmarkReport,
    BenchmarkTaskSpec,
    ConfigReport,
    TrialRecord,
)
from loopforge.domain.context_lifecycle import ContextAccounting
from loopforge.domain.events import (
    BudgetDebited,
    DomainEvent,
    Event,
    RunStarted,
    RunStopped,
)
from loopforge.domain.types import RunStatus

_MAX_ID_LENGTH = 128


def _validate_id(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = f"{field_name} cannot be empty"
        raise ValueError(msg)
    if len(value) > _MAX_ID_LENGTH or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        msg_2 = f"{field_name} must not contain control characters or exceed 128 characters"
        raise ValueError(msg_2)


def _validate_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = f"{field_name} must be an integer"
        raise ValueError(msg)  # noqa: TRY004
    if value <= 0:
        msg_2 = f"{field_name} must be positive"
        raise ValueError(msg_2)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvalConfiguration:
    """One harness configuration under evaluation — what Pareto reports compare.

    Application-level, data-only descriptive wiring: runtime CONFIGURATIONS,
    not model brands. The M6 entrypoints driver interprets these fields when
    building each trial's runtime: the budget ceilings (``max_cost_usd``,
    ``max_iterations``), the no-progress stall threshold, the verification
    cadence (``verify_read_only_turns`` — False keeps the PACS-016 M8 tuned
    default where read-only turns skip verification, True restores the
    legacy every-turn cadence for A/B comparison), whether the routing
    policy is wired, and the expensive-model set the trajectory metric's
    ``expensive_model_turns`` counts against (``(provider, model)`` pairs).

    Every field may only NARROW or SELECT within authority the operator
    already granted — a configuration can tighten a budget or disable the
    router, never expand budgets, permissions, sandbox capabilities, or
    stopping rules beyond the granted envelope (AGENTS.md rules 11, 12).
    """

    config_id: str
    max_cost_usd: float
    max_iterations: int
    no_progress_limit: int = 3
    router_enabled: bool = False
    verify_read_only_turns: bool = False
    expensive_models: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        _validate_id(self.config_id, field_name="config_id")
        if not math.isfinite(self.max_cost_usd):
            msg = "max_cost_usd must be finite"
            raise ValueError(msg)
        if self.max_cost_usd <= 0:
            msg_2 = "max_cost_usd must be positive"
            raise ValueError(msg_2)
        _validate_positive_int(self.max_iterations, field_name="max_iterations")
        _validate_positive_int(self.no_progress_limit, field_name="no_progress_limit")
        if not isinstance(self.router_enabled, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_3 = "router_enabled must be a bool"
            raise TypeError(msg_3)
        if not isinstance(self.verify_read_only_turns, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_5 = "verify_read_only_turns must be a bool"
            raise TypeError(msg_5)
        for entry in self.expensive_models:
            if (
                not isinstance(entry, tuple)  # pyright: ignore[reportUnnecessaryIsInstance]
                or len(entry) != 2
                or any(not isinstance(part, str) or not part.strip() for part in entry)  # pyright: ignore[reportUnnecessaryIsInstance]
            ):
                msg_4 = "expensive_models entries must be (provider, model) non-empty string pairs"
                raise ValueError(msg_4)


@dataclass(frozen=True, slots=True, kw_only=True)
class TrialRunResult:
    """Everything a ``TrialDriver`` returns for one executed trial.

    ``status`` is the runtime's authoritative (terminal or wedged) run status
    from the final replayed state — the runner does not re-derive it because
    a non-terminal stream's status is not readable from the events alone.
    ``events`` is the full durable stream; ``evidence`` the operator-owned
    end-of-run workspace projection the M3 graders consume;
    ``context_accountings`` the per-assembly ledgers the M4 context metrics
    prefer (empty when the driver collected none — the projection honestly
    degrades to billed usage).
    """

    run_id: str
    status: RunStatus
    events: tuple[Event, ...]
    evidence: GraderEvidence
    context_accountings: tuple[ContextAccounting, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.run_id, field_name="run_id")
        if not isinstance(self.status, RunStatus):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "trial result status must be a RunStatus"
            raise TypeError(msg)
        for event in self.events:
            # Boundary validation is intentional: drivers are caller wiring.
            if not isinstance(event, DomainEvent):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg_2 = "trial result events must be domain events"
                raise TypeError(msg_2)
        if not isinstance(self.evidence, GraderEvidence):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_3 = "trial result evidence must be a GraderEvidence"
            raise TypeError(msg_3)
        for accounting in self.context_accountings:
            if not isinstance(accounting, ContextAccounting):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg_4 = "trial result context_accountings must be ContextAccounting instances"
                raise TypeError(msg_4)


class TrialDriver(Protocol):
    """Executes one trial of one task under one configuration.

    The M6 entrypoints driver builds the workload bundle for the task, wires
    the runtime per the ``EvalConfiguration``, drives the run to quiescence,
    collects the grader evidence, and closes the bundle. This seam is what
    keeps the runner layer-clean: workloads and adapters never cross into
    the application layer.
    """

    def __call__(
        self, spec: BenchmarkTaskSpec, config: EvalConfiguration, trial_id: str
    ) -> TrialRunResult: ...


class TrialDriverContractError(TypeError):
    """Raised when a trial driver violates the runner's return contract."""


@dataclass(frozen=True, slots=True)
class _GradedTrial:
    """One trial's domain record plus its M3 success classification."""

    record: TrialRecord
    success: bool
    false_success: bool


def _mean(values: Iterable[float]) -> float:
    collected = tuple(values)
    return sum(collected) / len(collected)


def _total_cost_usd(events: tuple[Event, ...]) -> float:
    """Billed cost: sum of durable ``BudgetDebited`` usage (transient retries never persist)."""
    return sum(event.usage.cost_usd for event in events if isinstance(event, BudgetDebited))


def _total_tokens(events: tuple[Event, ...]) -> int:
    """Billed tokens, mirroring ``RunState.total_tokens`` (input + output only)."""
    return sum(
        event.usage.input_tokens + event.usage.output_tokens
        for event in events
        if isinstance(event, BudgetDebited)
    )


def _latency_seconds(events: tuple[Event, ...]) -> float:
    """Wall-clock latency from the durable stream's own timestamps.

    Terminal streams measure ``RunStarted`` → trailing ``RunStopped``. A
    wedged or otherwise non-terminal stream (which the M3 graders already
    classify as not-success) falls back to the first → last event span, so
    the record carries an honest duration for the work that did happen. An
    empty stream has no measurable duration: 0.0.
    """
    if not events:
        return 0.0
    started = next((event for event in events if isinstance(event, RunStarted)), None)
    stopped = events[-1] if isinstance(events[-1], RunStopped) else None
    if started is not None and stopped is not None:
        delta = (stopped.occurred_at - started.occurred_at).total_seconds()
    else:
        delta = (events[-1].occurred_at - events[0].occurred_at).total_seconds()
    # Defensive floor: event timestamps are authoritative but a misbehaving
    # clock must never produce a negative duration the domain record rejects.
    return max(0.0, delta)


def _grade_trial(
    spec: BenchmarkTaskSpec,
    config: EvalConfiguration,
    trial_id: str,
    result: TrialRunResult,
) -> _GradedTrial:
    events = result.events
    results = grade_trial(spec, events, result.evidence)
    metrics = compute_trajectory_metrics(
        spec,
        events,
        expensive_models=config.expensive_models,
        context_accountings=result.context_accountings,
    )
    record = TrialRecord(
        trial_id=trial_id,
        task_id=spec.task_id,
        config_id=config.config_id,
        run_id=result.run_id,
        status=result.status,
        metrics=metrics,
        grader_results=results,
        cost_usd=_total_cost_usd(events),
        total_tokens=_total_tokens(events),
        latency_seconds=_latency_seconds(events),
        human_interventions=count_human_interventions(events),
    )
    return _GradedTrial(
        record=record,
        success=trial_is_success(results, events),
        false_success=trial_is_false_success(results, events),
    )


def _aggregate(trials: tuple[_GradedTrial, ...]) -> tuple[ConfigReport, ...]:
    """One ``ConfigReport`` per (config, task) pair, deterministically ordered."""
    groups: dict[tuple[str, str], list[_GradedTrial]] = {}
    for trial in trials:
        key = (trial.record.config_id, trial.record.task_id)
        groups.setdefault(key, []).append(trial)
    reports: list[ConfigReport] = []
    for config_id, task_id in sorted(groups):
        group = groups[(config_id, task_id)]
        count = len(group)
        successes = sum(1 for trial in group if trial.success)
        false_successes = sum(1 for trial in group if trial.false_success)
        reports.append(
            ConfigReport(
                config_id=config_id,
                task_id=task_id,
                trials=count,
                successes=successes,
                false_successes=false_successes,
                success_rate=successes / count,
                false_success_rate=false_successes / count,
                mean_cost_usd=_mean(trial.record.cost_usd for trial in group),
                mean_latency_seconds=_mean(trial.record.latency_seconds for trial in group),
                mean_total_tokens=_mean(float(trial.record.total_tokens) for trial in group),
                mean_human_interventions=_mean(
                    float(trial.record.human_interventions) for trial in group
                ),
            )
        )
    return tuple(reports)


@dataclass(frozen=True, slots=True)
class _ConfigSummary:
    """Config-level dominance vector: means across that config's task reports."""

    config_id: str
    success_rate: float
    false_success_rate: float
    mean_cost_usd: float
    mean_latency_seconds: float
    mean_human_interventions: float


def _config_summaries(reports: tuple[ConfigReport, ...]) -> tuple[_ConfigSummary, ...]:
    """Per-config dominance vectors: means over that config's task reports.

    COMPARABILITY CAVEAT: each mean covers whatever tasks that config
    appears against in ``reports``, so dominance comparisons between two
    configs are only meaningful when both have IDENTICAL task coverage.
    ``run_trials`` guarantees uniform coverage (it iterates the full
    config x task cross product), and ``BenchmarkReport.config_reports``
    keeps the per-(config, task) breakdown so consumers can always verify
    coverage before comparing; subset runs assembled outside ``run_trials``
    must check it themselves.
    """
    groups: dict[str, list[ConfigReport]] = {}
    for report in reports:
        groups.setdefault(report.config_id, []).append(report)
    return tuple(
        _ConfigSummary(
            config_id=config_id,
            success_rate=_mean(report.success_rate for report in groups[config_id]),
            false_success_rate=_mean(report.false_success_rate for report in groups[config_id]),
            mean_cost_usd=_mean(report.mean_cost_usd for report in groups[config_id]),
            mean_latency_seconds=_mean(report.mean_latency_seconds for report in groups[config_id]),
            mean_human_interventions=_mean(
                report.mean_human_interventions for report in groups[config_id]
            ),
        )
        for config_id in sorted(groups)
    )


def _dominates(a: _ConfigSummary, b: _ConfigSummary) -> bool:
    """Pareto dominance: A is at least as good on every axis and strictly better on one.

    Axes: success rate (higher is better); false-success rate, mean cost,
    mean latency, mean human interventions (lower is better). Ties —
    identical summary vectors — do NOT dominate each other, so every tied
    configuration stays on the frontier.
    """
    no_worse = (
        a.success_rate >= b.success_rate
        and a.false_success_rate <= b.false_success_rate
        and a.mean_cost_usd <= b.mean_cost_usd
        and a.mean_latency_seconds <= b.mean_latency_seconds
        and a.mean_human_interventions <= b.mean_human_interventions
    )
    strictly_better = (
        a.success_rate > b.success_rate
        or a.false_success_rate < b.false_success_rate
        or a.mean_cost_usd < b.mean_cost_usd
        or a.mean_latency_seconds < b.mean_latency_seconds
        or a.mean_human_interventions < b.mean_human_interventions
    )
    return no_worse and strictly_better


def _pareto_config_ids(reports: tuple[ConfigReport, ...]) -> tuple[str, ...]:
    """The non-dominated configuration set, sorted by config_id."""
    summaries = _config_summaries(reports)
    return tuple(
        candidate.config_id
        for candidate in summaries
        if not any(
            other.config_id != candidate.config_id and _dominates(other, candidate)
            for other in summaries
        )
    )


def _validate_inputs(
    specs: tuple[BenchmarkTaskSpec, ...],
    configs: tuple[EvalConfiguration, ...],
    trials_per_task: int,
) -> None:
    if isinstance(trials_per_task, bool) or not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
        trials_per_task, int
    ):
        msg = "trials_per_task must be an integer"
        raise ValueError(msg)  # noqa: TRY004
    if trials_per_task < 1:
        msg_2 = "trials_per_task must be at least 1"
        raise ValueError(msg_2)
    if not specs:
        msg_3 = "benchmark specs cannot be empty"
        raise ValueError(msg_3)
    for spec in specs:
        if not isinstance(spec, BenchmarkTaskSpec):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_4 = "benchmark specs must be BenchmarkTaskSpec instances"
            raise TypeError(msg_4)
    task_ids = [spec.task_id for spec in specs]
    if len(set(task_ids)) != len(task_ids):
        msg_5 = "benchmark spec task_ids must be unique"
        raise ValueError(msg_5)
    if not configs:
        msg_6 = "eval configurations cannot be empty"
        raise ValueError(msg_6)
    for config in configs:
        if not isinstance(config, EvalConfiguration):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_7 = "eval configurations must be EvalConfiguration instances"
            raise TypeError(msg_7)
    config_ids = [config.config_id for config in configs]
    if len(set(config_ids)) != len(config_ids):
        msg_8 = "eval configuration config_ids must be unique"
        raise ValueError(msg_8)


def run_trials(  # noqa: PLR0913 - the report identity is explicit operator wiring
    specs: tuple[BenchmarkTaskSpec, ...],
    configs: tuple[EvalConfiguration, ...],
    trials_per_task: int,
    driver: TrialDriver,
    *,
    report_id: str,
    suite_version: str,
    lock_hash: str,
) -> BenchmarkReport:
    """Run every (config, spec) pair for ``trials_per_task`` trials and aggregate.

    Trial ids are deterministic: ``f"{config_id}:{task_id}:{index}"`` with
    zero-based indices, iterated config-major then task-id order (both sorted
    by id), so the same inputs always produce the same trial sequence.
    Coverage is the FULL config x task cross product: every configuration is
    evaluated against every task, which is what makes the Pareto dominance
    vectors commensurable (see ``_config_summaries`` for the caveat that
    applies to reports assembled any other way; consumers can always inspect
    per-config task coverage via ``BenchmarkReport.config_reports``).
    ``suite_version`` and ``lock_hash`` are echoed verbatim from the locked
    suite the caller evaluated against — the runner never recomputes or
    alters benchmark content, and the domain report refuses a malformed
    hash. The Pareto frontier follows ``_dominates``: ties never dominate,
    so identical configurations all remain on the frontier.
    """
    _validate_inputs(specs, configs, trials_per_task)
    ordered_configs = sorted(configs, key=lambda config: config.config_id)
    ordered_specs = sorted(specs, key=lambda spec: spec.task_id)
    trials: list[_GradedTrial] = []
    for config in ordered_configs:
        for spec in ordered_specs:
            for index in range(trials_per_task):
                trial_id = f"{config.config_id}:{spec.task_id}:{index}"
                result = driver(spec, config, trial_id)
                # Boundary validation is intentional: drivers are caller wiring.
                if not isinstance(result, TrialRunResult):  # pyright: ignore[reportUnnecessaryIsInstance]
                    msg = f"trial driver returned {type(result).__name__}, expected TrialRunResult"
                    raise TrialDriverContractError(msg)
                # The stream must belong to the trial's own run: a driver
                # that mixes in another run's events would silently grade
                # and meter foreign work as this trial's.
                for event in result.events:
                    if event.run_id != result.run_id:
                        msg_2 = (
                            f"trial driver returned an event with run_id "
                            f"{event.run_id!r} that does not match the trial "
                            f"run_id {result.run_id!r}"
                        )
                        raise TrialDriverContractError(msg_2)
                trials.append(_grade_trial(spec, config, trial_id, result))
    config_reports = _aggregate(tuple(trials))
    return BenchmarkReport(
        report_id=report_id,
        suite_version=suite_version,
        lock_hash=lock_hash,
        config_reports=config_reports,
        pareto_config_ids=_pareto_config_ids(config_reports),
    )
