"""Statistical candidate-policy heuristics (PACS-017 M8).

A deterministic, pure derivation over operator-owned eval evidence:
stored benchmark reports (plus optional shadowed context-budget samples)
are reduced to a *suggested* candidate ``ExecutionPolicy``. The
suggestion is always built through the domain constructors and clamped
into code-owned envelopes, so an out-of-envelope suggestion is
impossible by construction — and the result is only ever registered as
a CANDIDATE with a referenced evidence basis. Nothing here applies or
promotes anything: authority stays with the operator (rule 16,
ADR-0012), exactly like a hand-authored candidate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from loopforge.domain.benchmarks import BenchmarkReport
from loopforge.domain.policies import (
    MAX_POLICY_TEXT,
    ContextAllocationBounds,
    ExecutionPolicy,
    PolicyRoutingKnobs,
)

# Code-owned derivation envelopes: the heuristics may move knobs only
# inside these bounds; clamping is explicit and reported in the rationale.
CTX_STEP: Final = 512
CTX_CEILING_MIN: Final = 1024
CTX_CEILING_MAX: Final = 16384
CTX_RESERVE: Final = 256
CTX_FLOOR_MIN: Final = 512
CTX_FLOOR_FRACTION: Final = 0.25
P90_RANK: Final = 0.9
STALL_DEFAULT: Final = 2
STALL_MIN: Final = 1
STALL_MAX: Final = 8
RECOVERY_PATIENT: Final = 0.5
RECOVERY_VERY_PATIENT: Final = 1.5


class HeuristicDerivationError(ValueError):
    """The evidence base cannot support a suggestion — fail closed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateSuggestion:
    """A derived candidate policy plus its auditable derivation trail."""

    policy: ExecutionPolicy
    evidence_basis: str
    rationale: tuple[str, ...]


def _p90(values: list[float]) -> float:
    """Nearest-rank p90 — deterministic for identical inputs."""
    ordered = sorted(values)
    return ordered[math.ceil(P90_RANK * len(ordered)) - 1]


def _round_up(value: float, step: int) -> int:
    return math.ceil(value / step) * step


def _derive_ceiling(samples: list[float]) -> tuple[int, str]:
    peak = _p90(samples)
    ceiling = min(CTX_CEILING_MAX, max(CTX_CEILING_MIN, _round_up(peak, CTX_STEP)))
    rationale = (
        f"context ceiling {ceiling} from p90 context tokens {peak:.1f} "
        f"across {len(samples)} sample(s)"
    )
    if ceiling != _round_up(peak, CTX_STEP):
        rationale += f" (clamped into [{CTX_CEILING_MIN}, {CTX_CEILING_MAX}])"
    return ceiling, rationale


def _derive_stall_threshold(mean_recovery: float, rows: int) -> tuple[int, str]:
    threshold = STALL_DEFAULT
    if mean_recovery >= RECOVERY_VERY_PATIENT:
        threshold = STALL_DEFAULT + 2
    elif mean_recovery >= RECOVERY_PATIENT:
        threshold = STALL_DEFAULT + 1
    threshold = min(STALL_MAX, max(STALL_MIN, threshold))
    rationale = (
        f"stall escalation threshold {threshold} from mean recovery events "
        f"{mean_recovery:.2f} across {rows} report row(s)"
    )
    return threshold, rationale


def derive_candidate_policy(
    reports: tuple[BenchmarkReport, ...],
    *,
    policy_id: str,
    version: int,
    shadow_budget_tokens: tuple[int, ...] = (),
) -> CandidateSuggestion:
    """Derive a suggested candidate policy from stored eval evidence.

    Deterministic: identical inputs always produce the identical
    suggestion. Fails closed (``HeuristicDerivationError``) when there
    are no reports or the evidence carries no positive context-token
    samples (for example schema-v1 reports, which predate the axis).
    Every knob is clamped into its code-owned envelope and the result is
    constructed through ``ExecutionPolicy`` — the domain validation is
    the final fail-closed gate.
    """
    if not reports:
        msg = "cannot derive a candidate policy without eval reports"
        raise HeuristicDerivationError(msg)
    rows = [row for report in reports for row in report.config_reports]
    ctx_samples = [
        row.mean_context_tokens_used for row in rows if row.mean_context_tokens_used > 0.0
    ]
    ctx_samples.extend(float(sample) for sample in shadow_budget_tokens)
    if not ctx_samples:
        msg_2 = "eval evidence carries no context-token samples (schema v1 reports?)"
        raise HeuristicDerivationError(msg_2)
    ceiling, ceiling_rationale = _derive_ceiling(ctx_samples)
    floor = max(CTX_FLOOR_MIN, int(ceiling * CTX_FLOOR_FRACTION) // CTX_STEP * CTX_STEP)
    total_trials = sum(row.trials for row in rows)
    mean_recovery = sum(row.mean_recovery_events * row.trials for row in rows) / total_trials
    threshold, stall_rationale = _derive_stall_threshold(mean_recovery, len(rows))
    report_ids = sorted({report.report_id for report in reports})
    basis = f"heuristic derivation from eval reports: {', '.join(report_ids)}"
    if shadow_budget_tokens:
        basis += f" (+{len(shadow_budget_tokens)} shadow samples)"
    if len(basis) > MAX_POLICY_TEXT:
        basis = f"{basis[: MAX_POLICY_TEXT - 3]}..."
    policy = ExecutionPolicy(
        policy_id=policy_id,
        version=version,
        routing=PolicyRoutingKnobs(stall_escalation_threshold=threshold),
        context_allocation=ContextAllocationBounds(
            floor_tokens=floor,
            ceiling_tokens=ceiling,
            reserve_tokens=CTX_RESERVE,
            step_tokens=CTX_STEP,
        ),
    )
    return CandidateSuggestion(
        policy=policy,
        evidence_basis=basis,
        rationale=(ceiling_rationale, stall_rationale),
    )
