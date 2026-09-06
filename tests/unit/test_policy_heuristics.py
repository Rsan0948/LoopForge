"""Statistical candidate-policy heuristic tests (PACS-017 M8).

The derivation is bounded, deterministic, and authority-gated: it only
ever produces a CANDIDATE suggestion with a referenced evidence basis —
never an applied or promoted policy. Allow+deny pairs per rule 10: every
derivation rule is pinned on both sides, and out-of-envelope suggestions
are impossible by construction (clamped + domain-validated).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.application.policy_heuristics import (
    CTX_CEILING_MAX,
    CTX_CEILING_MIN,
    HeuristicDerivationError,
    derive_candidate_policy,
)
from loopforge.domain.benchmarks import BenchmarkReport, ConfigReport
from loopforge.domain.events import PlanCreated, RunStarted, ShadowDecisionRecorded
from loopforge.domain.policies import (
    MAX_POLICY_TEXT,
    PolicyLifecycle,
    ShadowDecisionKind,
)
from loopforge.domain.types import EventId, RunId
from loopforge.entrypoints.policy import PolicyRegistryStore, shadow_budget_samples

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
LOCK_HASH = "0123456789abcdef" * 4


def _row(
    config_id: str,
    task_id: str,
    *,
    ctx_tokens: float,
    recovery: float,
    trials: int = 2,
) -> ConfigReport:
    return ConfigReport(
        config_id=config_id,
        task_id=task_id,
        trials=trials,
        successes=trials,
        false_successes=0,
        success_rate=1.0,
        false_success_rate=0.0,
        mean_cost_usd=0.02,
        mean_latency_seconds=1.0,
        mean_total_tokens=240.0,
        mean_human_interventions=0.0,
        mean_context_tokens_used=ctx_tokens,
        mean_context_items_dropped=0.0,
        mean_recovery_events=recovery,
    )


def _report(
    report_id: str = "eval-a",
    rows: tuple[ConfigReport, ...] = (_row("cfg", "task", ctx_tokens=2000.0, recovery=0.0),),
) -> BenchmarkReport:
    return BenchmarkReport(
        report_id=report_id,
        suite_version="1.0.0",
        lock_hash=LOCK_HASH,
        config_reports=rows,
        pareto_config_ids=("cfg",),
    )


# --- Derivation: allow ---------------------------------------------------------


def test_derivation_sizes_the_ceiling_from_the_p90_sample() -> None:
    reports = (
        _report(
            rows=(
                _row("cfg", "t1", ctx_tokens=100.0, recovery=0.0),
                _row("cfg", "t2", ctx_tokens=2000.0, recovery=0.0),
                _row("cfg", "t3", ctx_tokens=3000.0, recovery=0.0),
            )
        ),
    )
    suggestion = derive_candidate_policy(reports, policy_id="derived", version=1)
    # Nearest-rank p90 of [100, 2000, 3000] is 3000 → 3072 rounded up to 512.
    assert suggestion.policy.context_allocation.ceiling_tokens == 3072
    assert suggestion.policy.context_allocation.floor_tokens == 512
    assert suggestion.policy.context_allocation.step_tokens == 512
    assert suggestion.policy.context_allocation.reserve_tokens == 256


def test_derivation_is_deterministic_for_identical_inputs() -> None:
    reports = (_report(), _report("eval-b"))
    first = derive_candidate_policy(reports, policy_id="derived", version=1)
    second = derive_candidate_policy(reports, policy_id="derived", version=1)
    assert first == second


def test_evidence_basis_references_every_contributing_report() -> None:
    reports = (_report("eval-b"), _report("eval-a"))
    suggestion = derive_candidate_policy(reports, policy_id="derived", version=1)
    assert suggestion.evidence_basis == "heuristic derivation from eval reports: eval-a, eval-b"
    assert suggestion.rationale


def test_shadow_samples_join_the_distribution_and_the_basis() -> None:
    reports = (
        _report(
            rows=(
                _row("cfg", "t1", ctx_tokens=1024.0, recovery=0.0),
                _row("cfg", "t2", ctx_tokens=1024.0, recovery=0.0),
            )
        ),
    )
    suggestion = derive_candidate_policy(
        reports, policy_id="derived", version=1, shadow_budget_tokens=(8192,)
    )
    # p90 of [1024, 1024, 8192] is 8192 — the shadow evidence moved the knob.
    assert suggestion.policy.context_allocation.ceiling_tokens == 8192
    assert suggestion.evidence_basis.endswith("(+1 shadow samples)")


@pytest.mark.parametrize(
    ("recovery", "expected"),
    [(0.0, 2), (0.49, 2), (0.5, 3), (1.49, 3), (1.5, 4), (9.0, 4)],
)
def test_stall_threshold_tracks_mean_recovery_events(recovery: float, expected: int) -> None:
    suggestion = derive_candidate_policy(
        (_report(rows=(_row("cfg", "t", ctx_tokens=2000.0, recovery=recovery),)),),
        policy_id="derived",
        version=1,
    )
    assert suggestion.policy.routing.stall_escalation_threshold == expected


def test_recovery_mean_is_weighted_by_trials() -> None:
    rows = (
        _row("cfg", "t1", ctx_tokens=2000.0, recovery=0.0, trials=3),
        _row("cfg", "t2", ctx_tokens=2000.0, recovery=3.0, trials=1),
    )
    suggestion = derive_candidate_policy((_report(rows=rows),), policy_id="derived", version=1)
    # Weighted mean: (0*3 + 3*1)/4 = 0.75 → patient threshold 3, not 4.
    assert suggestion.policy.routing.stall_escalation_threshold == 3


def test_zero_filled_v1_rows_do_not_dilute_the_recovery_mean() -> None:
    # M9 (B4): schema-v1 rows carry zero-FILLED v2 means (unknown, not a
    # measured zero) — pooling them would silently dilute the mean. The
    # heuristic excludes them and the rationale names the exclusion.
    rows = (
        _row("cfg", "t1", ctx_tokens=2000.0, recovery=1.5),
        _row("cfg", "t2", ctx_tokens=0.0, recovery=0.0),  # v1-style zero-fill
    )
    suggestion = derive_candidate_policy((_report(rows=rows),), policy_id="derived", version=1)
    # Pooled, the mean would be 0.75 → threshold 3; undiluted 1.5 → 4.
    assert suggestion.policy.routing.stall_escalation_threshold == 4
    assert any(
        "1 schema-v1 row(s) without measured axes excluded" in line for line in suggestion.rationale
    )


# --- Derivation: deny / clamp ---------------------------------------------------


def test_empty_evidence_fails_closed() -> None:
    with pytest.raises(HeuristicDerivationError, match="without eval reports"):
        derive_candidate_policy((), policy_id="derived", version=1)


def test_v1_reports_without_context_samples_fail_closed() -> None:
    # Schema-v1 artifacts carry zero-filled context means: honestly no data.
    reports = (_report(rows=(_row("cfg", "t", ctx_tokens=0.0, recovery=1.0),)),)
    with pytest.raises(HeuristicDerivationError, match="no context-token samples"):
        derive_candidate_policy(reports, policy_id="derived", version=1)


def test_oversized_samples_clamp_into_the_ceiling_envelope() -> None:
    reports = (_report(rows=(_row("cfg", "t", ctx_tokens=1_000_000.0, recovery=0.0),)),)
    suggestion = derive_candidate_policy(reports, policy_id="derived", version=1)
    assert suggestion.policy.context_allocation.ceiling_tokens == CTX_CEILING_MAX
    assert "clamped" in suggestion.rationale[0]


def test_tiny_samples_clamp_into_the_ceiling_envelope() -> None:
    reports = (_report(rows=(_row("cfg", "t", ctx_tokens=10.0, recovery=0.0),)),)
    suggestion = derive_candidate_policy(reports, policy_id="derived", version=1)
    assert suggestion.policy.context_allocation.ceiling_tokens == CTX_CEILING_MIN
    # The floor never exceeds the ceiling, even at the bottom of the envelope.
    assert (
        suggestion.policy.context_allocation.floor_tokens
        <= suggestion.policy.context_allocation.ceiling_tokens
    )


def test_the_evidence_basis_stays_bounded() -> None:
    reports = tuple(_report(f"eval-report-{index:03d}") for index in range(40))
    suggestion = derive_candidate_policy(reports, policy_id="derived", version=1)
    assert len(suggestion.evidence_basis) <= MAX_POLICY_TEXT
    assert suggestion.evidence_basis.endswith("...")


def test_an_invalid_policy_id_is_rejected_by_the_domain_gate() -> None:
    with pytest.raises(ValueError, match="policy_id"):
        derive_candidate_policy((_report(),), policy_id="a/b", version=1)


# --- Authority gate: suggestions are only ever CANDIDATEs ------------------------


def test_a_registered_suggestion_is_a_candidate_never_promoted(tmp_path: Path) -> None:
    registry = PolicyRegistryStore(tmp_path)
    suggestion = derive_candidate_policy((_report(),), policy_id="derived", version=1)
    record = registry.register(suggestion.policy, evidence_basis=suggestion.evidence_basis)
    assert record.lifecycle is PolicyLifecycle.CANDIDATE
    assert registry.promoted() == ()


# --- Shadow evidence extraction ---------------------------------------------------

RUN = RunId("run_heuristics")


def _seed_shadow_store(store: InMemoryEventStore | SQLiteEventStore) -> None:
    store.append(
        RunStarted(
            event_id=EventId("e1"), run_id=RUN, occurred_at=NOW, sequence=1, objective="probe"
        ),
        expected_version=0,
    )
    store.append(
        PlanCreated(event_id=EventId("e2"), run_id=RUN, occurred_at=NOW, sequence=2, plan="p"),
        expected_version=1,
    )
    store.append(
        ShadowDecisionRecorded(
            event_id=EventId("e3"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=3,
            policy_id="candidate-shadow",
            policy_version=2,
            kind=ShadowDecisionKind.CONTEXT_BUDGET,
            decision="max_tokens=2048 reserve_tokens=256",
            basis="dropped_over_budget=False utilization_fraction=0.5",
        ),
        expected_version=2,
    )
    store.append(
        ShadowDecisionRecorded(
            event_id=EventId("e4"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=4,
            policy_id="candidate-shadow",
            policy_version=2,
            kind=ShadowDecisionKind.MODEL_ROUTE,
            decision="scripted/advanced-model tier=advanced",
            basis="reason_code=ROUTE_INITIAL_SELECTION",
        ),
        expected_version=3,
    )


def test_shadow_budget_samples_extract_context_decisions_only() -> None:
    store = InMemoryEventStore()
    _seed_shadow_store(store)
    assert shadow_budget_samples(store) == (2048,)


def test_shadow_budget_samples_survive_the_durable_store(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db", codec=JsonEventCodec())
    _seed_shadow_store(store)
    assert shadow_budget_samples(store) == (2048,)


def test_shadow_budget_samples_skip_unparseable_decisions() -> None:
    store = InMemoryEventStore()
    store.append(
        RunStarted(
            event_id=EventId("e1"), run_id=RUN, occurred_at=NOW, sequence=1, objective="probe"
        ),
        expected_version=0,
    )
    store.append(
        PlanCreated(event_id=EventId("e2"), run_id=RUN, occurred_at=NOW, sequence=2, plan="p"),
        expected_version=1,
    )
    store.append(
        ShadowDecisionRecorded(
            event_id=EventId("e3"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=3,
            policy_id="candidate-shadow",
            policy_version=2,
            kind=ShadowDecisionKind.CONTEXT_BUDGET,
            decision="no-compatible-model",
            basis="accounting=absent",
        ),
        expected_version=2,
    )
    assert shadow_budget_samples(store) == ()
