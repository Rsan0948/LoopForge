"""Locked benchmark suite and multi-trial evaluation vocabulary (PACS-016, M1).

Benchmark tasks, graders, and fixtures are OPERATOR-OWNED authority: the
harness policy under test may never tune, reweight, or redefine them
(AGENTS.md rules 12, 14, 16). The suite is LOCKED by content hash —
``suite_lock_hash`` canonicalizes every semantic field of every task, and
``BenchmarkSuite`` refuses to exist when its stored hash disagrees with its
task content, so any benchmark drift is loud at construction time. False
success is first-class: ``GraderVerdict.FALSE_SUCCESS`` records "graders
passed while the task objective was not actually met" — the failure mode
this laboratory exists to measure. This module is pure domain vocabulary:
fixture builders land in ``loopforge.workloads`` (M2), the deterministic
graders behind ``GraderId`` in M3, and the computations that fill
``TrajectoryMetrics`` in M4.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum

from loopforge.domain.types import RunStatus
from loopforge.domain.workspace import PatchConstraints

_MAX_ID_LENGTH = 128
_MAX_OBJECTIVE_LENGTH = 2000
_MAX_GRADER_DETAIL_LENGTH = 300
_RATE_EPSILON = 1e-9
_HEX_DIGITS = frozenset("0123456789abcdef")


class BenchmarkCategory(StrEnum):
    """Closed, code-owned vocabulary of benchmark task categories."""

    SIMPLE_BUG = "simple_bug"
    MULTI_FILE = "multi_file"
    MISLEADING_FAILURE = "misleading_failure"
    TRANSIENT_API = "transient_api"
    AMBIGUOUS_SUCCESS = "ambiguous_success"
    CONTEXT_POLLUTION = "context_pollution"
    STALE_STATE = "stale_state"
    STALL = "stall"
    PROMPT_INJECTION = "prompt_injection"
    HITL = "hitl"
    PARALLEL_WORK = "parallel_work"
    PROVIDER_OUTAGE = "provider_outage"


class BenchmarkSandboxMode(StrEnum):
    """Closed vocabulary of isolation modes a benchmark task may run under.

    ``TRUSTED_LOCAL`` is deliberately NOT strong isolation (AGENTS.md rule
    15): it is only for fixtures whose content is fully operator-authored.
    Untrusted or adversarial fixtures (e.g. prompt-injection tasks) must
    declare ``CONTAINER``.
    """

    TRUSTED_LOCAL = "trusted_local"
    CONTAINER = "container"


class GraderId(StrEnum):
    """Closed vocabulary of deterministic grader identities.

    The graders themselves land in M3; this module owns only their names so
    task specs can bind to them and trial records can attribute verdicts.
    """

    VERIFIED_SUCCESS = "verified_success"
    SCOPE_DISCIPLINE = "scope_discipline"
    GROUND_TRUTH = "ground_truth"
    RECOVERY = "recovery"


class GraderVerdict(StrEnum):
    """Closed vocabulary of single-grader verdicts on one trial.

    ``FALSE_SUCCESS`` is first-class: the grader pipeline passed while the
    task objective was not actually met.
    """

    PASS = "pass"
    FAIL = "fail"
    FALSE_SUCCESS = "false_success"


def _validate_id(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = f"{field_name} cannot be empty"
        raise ValueError(msg)
    if len(value) > _MAX_ID_LENGTH or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        msg_2 = f"{field_name} must not contain control characters or exceed 128 characters"
        raise ValueError(msg_2)


def _validate_bounded_text(
    value: str, *, field_name: str, max_length: int, allow_empty: bool
) -> None:
    if not allow_empty and not value.strip():
        msg = f"{field_name} cannot be empty"
        raise ValueError(msg)
    if len(value) > max_length:
        msg_2 = f"{field_name} cannot exceed {max_length} characters"
        raise ValueError(msg_2)
    if any((ord(char) < 0x20 and char not in "\n\t") or ord(char) == 0x7F for char in value):
        msg_3 = f"{field_name} must not contain control characters"
        raise ValueError(msg_3)


def _validate_lock_hash(value: str, *, field_name: str) -> None:
    if not value:
        msg = f"{field_name} cannot be empty"
        raise ValueError(msg)
    if any(char not in _HEX_DIGITS for char in value):
        msg_2 = f"{field_name} must be a lowercase hex string"
        raise ValueError(msg_2)


def _validate_nonnegative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = f"{field_name} must be an integer"
        raise ValueError(msg)  # noqa: TRY004
    if value < 0:
        msg_2 = f"{field_name} cannot be negative"
        raise ValueError(msg_2)


def _validate_finite_nonnegative(value: float, *, field_name: str) -> None:
    if not math.isfinite(value):
        msg = f"{field_name} must be finite"
        raise ValueError(msg)
    if value < 0:
        msg_2 = f"{field_name} cannot be negative"
        raise ValueError(msg_2)


def _validate_unit_interval(value: float, *, field_name: str) -> None:
    if not math.isfinite(value):
        msg = f"{field_name} must be finite"
        raise ValueError(msg)
    if not 0.0 <= value <= 1.0:
        msg_2 = f"{field_name} must be in [0, 1]"
        raise ValueError(msg_2)


@dataclass(frozen=True, slots=True, kw_only=True)
class GraderResult:
    """One deterministic grader's verdict on one trial, with bounded detail."""

    grader_id: GraderId
    verdict: GraderVerdict
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.grader_id, GraderId):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "grader_id must be a GraderId"
            raise TypeError(msg)
        if not isinstance(self.verdict, GraderVerdict):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = "grader verdict must be a GraderVerdict"
            raise TypeError(msg_2)
        if not isinstance(self.detail, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_3 = "grader detail must be a string"
            raise TypeError(msg_3)
        _validate_bounded_text(
            self.detail,
            field_name="grader detail",
            max_length=_MAX_GRADER_DETAIL_LENGTH,
            allow_empty=True,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkTaskSpec:
    """Code-owned, operator-authoritative definition of one locked benchmark task.

    ``fixture_id`` binds to a code-owned fixture in ``loopforge.workloads``
    (M2); ``grader_ids`` names the deterministic graders that must classify
    every trial of this task; ``allowed_prefixes`` bounds the workspace paths
    a solution patch may touch with exactly the ``PatchConstraints``
    validation semantics. ``live_eligible`` is opt-in: a task may run in
    live-model evaluations only when explicitly marked.
    """

    task_id: str
    category: BenchmarkCategory
    objective: str
    fixture_id: str
    sandbox_mode: BenchmarkSandboxMode
    grader_ids: tuple[GraderId, ...]
    allowed_prefixes: tuple[str, ...] = ()
    live_eligible: bool = False

    def __post_init__(self) -> None:
        _validate_id(self.task_id, field_name="task_id")
        if not isinstance(self.category, BenchmarkCategory):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "benchmark task category must be a BenchmarkCategory"
            raise TypeError(msg)
        _validate_bounded_text(
            self.objective,
            field_name="benchmark task objective",
            max_length=_MAX_OBJECTIVE_LENGTH,
            allow_empty=False,
        )
        _validate_id(self.fixture_id, field_name="fixture_id")
        if not isinstance(self.sandbox_mode, BenchmarkSandboxMode):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = "benchmark sandbox mode must be a BenchmarkSandboxMode"
            raise TypeError(msg_2)
        if not self.grader_ids:
            msg_3 = "benchmark task must name at least one grader"
            raise ValueError(msg_3)
        for grader_id in self.grader_ids:
            if not isinstance(grader_id, GraderId):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg_4 = "benchmark task grader_ids must be GraderId members"
                raise TypeError(msg_4)
        if len(set(self.grader_ids)) != len(self.grader_ids):
            msg_5 = "benchmark task grader_ids must be unique"
            raise ValueError(msg_5)
        for prefix in self.allowed_prefixes:
            if not isinstance(prefix, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg_6 = "allowed path prefixes must be strings"
                raise TypeError(msg_6)
        # Delegate to the code-owned patch-scope contract so benchmark
        # prefixes share exactly the PatchConstraints validation semantics.
        PatchConstraints(allowed_prefixes=tuple(self.allowed_prefixes))
        if not isinstance(self.live_eligible, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_7 = "live_eligible must be a bool"
            raise TypeError(msg_7)


@dataclass(frozen=True, slots=True, kw_only=True)
class TrajectoryMetrics:
    """Numeric per-trial outputs the M4 trajectory analyzer computes."""

    model_turns: int = 0
    repetition_ratio: float = 0.0
    expensive_model_turns: int = 0
    scope_violations: int = 0
    permission_requests: int = 0
    context_tokens_used: int = 0
    context_items_dropped: int = 0
    recovery_events: int = 0

    def __post_init__(self) -> None:
        _validate_nonnegative_int(self.model_turns, field_name="model_turns")
        _validate_unit_interval(self.repetition_ratio, field_name="repetition_ratio")
        _validate_nonnegative_int(self.expensive_model_turns, field_name="expensive_model_turns")
        _validate_nonnegative_int(self.scope_violations, field_name="scope_violations")
        _validate_nonnegative_int(self.permission_requests, field_name="permission_requests")
        _validate_nonnegative_int(self.context_tokens_used, field_name="context_tokens_used")
        _validate_nonnegative_int(self.context_items_dropped, field_name="context_items_dropped")
        _validate_nonnegative_int(self.recovery_events, field_name="recovery_events")


@dataclass(frozen=True, slots=True, kw_only=True)
class TrialRecord:
    """One execution of one benchmark task under one harness configuration.

    ``status`` is the authoritative terminal (or aborted) run status from the
    runtime — never model self-report; ``grader_results`` preserves each
    grader's individual verdict so false-success attribution is auditable per
    grader.
    """

    trial_id: str
    task_id: str
    config_id: str
    run_id: str
    status: RunStatus
    metrics: TrajectoryMetrics
    grader_results: tuple[GraderResult, ...] = ()
    cost_usd: float = 0.0
    total_tokens: int = 0
    latency_seconds: float = 0.0
    human_interventions: int = 0

    def __post_init__(self) -> None:
        _validate_id(self.trial_id, field_name="trial_id")
        _validate_id(self.task_id, field_name="task_id")
        _validate_id(self.config_id, field_name="config_id")
        _validate_id(self.run_id, field_name="run_id")
        if not isinstance(self.status, RunStatus):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "trial status must be a RunStatus"
            raise TypeError(msg)
        if not isinstance(self.metrics, TrajectoryMetrics):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = "trial metrics must be a TrajectoryMetrics"
            raise TypeError(msg_2)
        for result in self.grader_results:
            if not isinstance(result, GraderResult):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg_3 = "trial grader results must be GraderResult instances"
                raise TypeError(msg_3)
        grader_ids = [result.grader_id for result in self.grader_results]
        if len(set(grader_ids)) != len(grader_ids):
            msg_4 = "trial grader results must have unique grader_ids"
            raise ValueError(msg_4)
        _validate_finite_nonnegative(self.cost_usd, field_name="trial cost_usd")
        _validate_nonnegative_int(self.total_tokens, field_name="trial total_tokens")
        _validate_finite_nonnegative(self.latency_seconds, field_name="trial latency_seconds")
        _validate_nonnegative_int(self.human_interventions, field_name="trial human_interventions")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfigReport:
    """Aggregated outcomes of one harness configuration on one benchmark task.

    The rates are stored (not derived) so reports round-trip exactly, but
    they are pinned consistent with the counts: a report whose
    ``success_rate`` disagrees with ``successes / trials`` beyond a small
    epsilon cannot exist. Success and false success are mutually exclusive
    per the M3 contract (a verifier-granted success is either real or false,
    never both), so their counts can never sum above ``trials``.
    """

    config_id: str
    task_id: str
    trials: int
    successes: int
    false_successes: int
    success_rate: float
    false_success_rate: float
    mean_cost_usd: float
    mean_latency_seconds: float
    mean_total_tokens: float
    mean_human_interventions: float

    def __post_init__(self) -> None:
        _validate_id(self.config_id, field_name="config_id")
        _validate_id(self.task_id, field_name="task_id")
        if isinstance(self.trials, bool) or not isinstance(self.trials, int):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "trials must be an integer"
            raise ValueError(msg)  # noqa: TRY004
        if self.trials <= 0:
            msg_2 = "trials must be positive"
            raise ValueError(msg_2)
        _validate_nonnegative_int(self.successes, field_name="successes")
        _validate_nonnegative_int(self.false_successes, field_name="false_successes")
        if self.successes > self.trials:
            msg_3 = "successes cannot exceed trials"
            raise ValueError(msg_3)
        if self.false_successes > self.trials:
            msg_4 = "false_successes cannot exceed trials"
            raise ValueError(msg_4)
        if self.successes + self.false_successes > self.trials:
            msg_7 = "successes + false_successes cannot exceed trials (mutually exclusive outcomes)"
            raise ValueError(msg_7)
        _validate_unit_interval(self.success_rate, field_name="success_rate")
        _validate_unit_interval(self.false_success_rate, field_name="false_success_rate")
        if abs(self.success_rate - self.successes / self.trials) > _RATE_EPSILON:
            msg_5 = "success_rate must equal successes / trials"
            raise ValueError(msg_5)
        if abs(self.false_success_rate - self.false_successes / self.trials) > _RATE_EPSILON:
            msg_6 = "false_success_rate must equal false_successes / trials"
            raise ValueError(msg_6)
        _validate_finite_nonnegative(self.mean_cost_usd, field_name="mean_cost_usd")
        _validate_finite_nonnegative(self.mean_latency_seconds, field_name="mean_latency_seconds")
        _validate_finite_nonnegative(self.mean_total_tokens, field_name="mean_total_tokens")
        _validate_finite_nonnegative(
            self.mean_human_interventions, field_name="mean_human_interventions"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkReport:
    """Immutable multi-config evaluation report against one locked suite.

    ``config_reports`` carries one ``ConfigReport`` per (config, task) pair:
    a configuration evaluated over several tasks appears once per task, so
    uniqueness is enforced on the pair, not on ``config_id`` alone (M5).
    ``pareto_config_ids`` must reference only configurations present in
    ``config_reports`` — a report pointing at unknown configurations cannot
    exist.
    """

    report_id: str
    suite_version: str
    lock_hash: str
    config_reports: tuple[ConfigReport, ...]
    pareto_config_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_id(self.report_id, field_name="report_id")
        if not self.suite_version.strip():
            msg = "suite_version cannot be empty"
            raise ValueError(msg)
        _validate_lock_hash(self.lock_hash, field_name="report lock_hash")
        if not self.config_reports:
            msg_2 = "a benchmark report must contain at least one config report"
            raise ValueError(msg_2)
        for report in self.config_reports:
            if not isinstance(report, ConfigReport):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg_3 = "benchmark report config_reports must be ConfigReport instances"
                raise TypeError(msg_3)
        config_ids = [report.config_id for report in self.config_reports]
        pairs = [(report.config_id, report.task_id) for report in self.config_reports]
        if len(set(pairs)) != len(pairs):
            msg_4 = (
                "benchmark report config_ids must be unique per task "
                "(one ConfigReport per (config_id, task_id) pair)"
            )
            raise ValueError(msg_4)
        if len(set(self.pareto_config_ids)) != len(self.pareto_config_ids):
            msg_5 = "pareto_config_ids must be unique"
            raise ValueError(msg_5)
        known = set(config_ids)
        for config_id in self.pareto_config_ids:
            if config_id not in known:
                msg_6 = f"pareto config {config_id!r} is not part of the report"
                raise ValueError(msg_6)


def _canonical_task(task: BenchmarkTaskSpec) -> dict[str, object]:
    """Canonical serialization of every field that defines task semantics."""
    return {
        "task_id": task.task_id,
        "category": task.category.value,
        "objective": task.objective,
        "fixture_id": task.fixture_id,
        "sandbox_mode": task.sandbox_mode.value,
        "grader_ids": [grader_id.value for grader_id in task.grader_ids],
        "allowed_prefixes": list(task.allowed_prefixes),
        "live_eligible": task.live_eligible,
    }


def suite_lock_hash(tasks: tuple[BenchmarkTaskSpec, ...]) -> str:
    """Deterministic lock hash over every field that defines suite semantics.

    Canonical JSON (sorted keys, compact separators, enum ``.value``, tasks
    sorted by ``task_id``) → sha256 hexdigest. Construction order of the task
    tuple is purely syntactic and never changes the hash; any semantic change
    to any task always does.
    """
    if not tasks:
        msg = "a locked suite must contain at least one task"
        raise ValueError(msg)
    for task in tasks:
        if not isinstance(task, BenchmarkTaskSpec):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = "suite tasks must be BenchmarkTaskSpec instances"
            raise TypeError(msg_2)
    task_ids = [task.task_id for task in tasks]
    if len(set(task_ids)) != len(task_ids):
        msg_3 = "suite task_ids must be unique"
        raise ValueError(msg_3)
    canonical = [_canonical_task(task) for task in sorted(tasks, key=lambda task: task.task_id)]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkSuite:
    """A locked, versioned set of benchmark tasks.

    ``lock_hash`` must equal ``suite_lock_hash(tasks)``: a suite object whose
    stored hash disagrees with its task content cannot exist — any semantic
    edit to the benchmark without an explicit re-lock is loud at construction.
    """

    version: str
    tasks: tuple[BenchmarkTaskSpec, ...]
    lock_hash: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            msg = "suite version cannot be empty"
            raise ValueError(msg)
        _validate_lock_hash(self.lock_hash, field_name="suite lock_hash")
        if self.lock_hash != suite_lock_hash(self.tasks):
            msg_2 = "suite lock_hash does not match the locked task content"
            raise ValueError(msg_2)
