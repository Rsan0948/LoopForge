"""Execution-policy vocabulary tests (PACS-017 M1).

Allow+deny pairs per AGENTS.md rule 10: every validation rule is pinned on
both the accepting and the rejecting side, and the authority boundary —
the immutable surface is unrepresentable — is pinned structurally.
"""

from __future__ import annotations

import dataclasses

import pytest

from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.policies import (
    ADAPTIVE_CONTEXT_POLICY,
    BASELINE_POLICY,
    MAX_POLICY_TEXT,
    MAX_POLICY_WORKERS,
    PATIENT_ROUTER_POLICY,
    ContextAllocationBounds,
    ExecutionPolicy,
    PolicyLifecycle,
    PolicyRecord,
    PolicyRoutingKnobs,
    UnknownPolicyError,
    builtin_policies,
    resolve_policy,
    transition_policy_record,
)
from loopforge.domain.routing import ModelRequirements, ModelTier, RoutingPolicyConfig


def _bounds(**overrides: object) -> ContextAllocationBounds:
    base: dict[str, object] = {"floor_tokens": 1024, "ceiling_tokens": 4096}
    base.update(overrides)
    return ContextAllocationBounds(**base)  # pyright: ignore[reportArgumentType]


def _policy(**overrides: object) -> ExecutionPolicy:
    base: dict[str, object] = {
        "policy_id": "candidate",
        "version": 1,
        "context_allocation": _bounds(),
    }
    base.update(overrides)
    return ExecutionPolicy(**base)  # pyright: ignore[reportArgumentType]


# --- ExecutionPolicy construction: allow ------------------------------------


def test_valid_policy_constructs_with_defaults() -> None:
    policy = _policy()
    assert policy.routing == PolicyRoutingKnobs()
    assert policy.verify_read_only_turns is False
    assert policy.worker_count is None


def test_initial_context_budget_is_the_floor() -> None:
    policy = _policy()
    assert policy.initial_context_budget() == ContextTokenBudget(max_tokens=1024, reserve_tokens=0)


def test_worker_count_allows_the_code_owned_envelope() -> None:
    assert _policy(worker_count=1).worker_count == 1
    assert _policy(worker_count=MAX_POLICY_WORKERS).worker_count == MAX_POLICY_WORKERS


# --- ExecutionPolicy construction: deny --------------------------------------


@pytest.mark.parametrize(
    "policy_id", ["", "   ", "-lead-dash", ".lead-dot", "has space", "a/b", "..", "x" * 65]
)
def test_policy_id_rejects_unsafe_shapes(policy_id: str) -> None:
    with pytest.raises(ValueError, match="policy_id"):
        _policy(policy_id=policy_id)


@pytest.mark.parametrize("version", [0, -1, True, 1.5, "1"])
def test_version_rejects_non_positive_and_non_integer(version: object) -> None:
    with pytest.raises(ValueError, match="version"):
        _policy(version=version)


@pytest.mark.parametrize("workers", [0, -1, True, MAX_POLICY_WORKERS + 1, 2.0])
def test_worker_count_rejects_outside_the_code_owned_envelope(workers: object) -> None:
    with pytest.raises(ValueError, match="worker_count"):
        _policy(worker_count=workers)


def test_immutable_authority_surface_is_unrepresentable() -> None:
    field_names = {field.name for field in dataclasses.fields(ExecutionPolicy)}
    forbidden = {
        "permissions",
        "granted",
        "budget",
        "max_cost_usd",
        "max_iterations",
        "no_progress_limit",
        "approval",
        "hitl",
        "sandbox",
        "secrets",
    }
    assert field_names.isdisjoint(forbidden)


# --- Routing knobs: allow + deny ---------------------------------------------


def test_routing_knobs_compose_with_workload_requirements() -> None:
    knobs = PolicyRoutingKnobs(
        default_tier=ModelTier.ADVANCED,
        stall_escalation_threshold=3,
        budget_pressure_remaining_fraction=0.25,
    )
    requirements = ModelRequirements(supports_tool_calls=True, min_context_window_tokens=4096)
    config = knobs.for_requirements(requirements)
    assert config == RoutingPolicyConfig(
        requirements=requirements,
        default_tier=ModelTier.ADVANCED,
        stall_escalation_threshold=3,
        budget_pressure_remaining_fraction=0.25,
    )


@pytest.mark.parametrize("threshold", [0, -2, True, 1.5])
def test_routing_knobs_reject_bad_stall_threshold(threshold: object) -> None:
    with pytest.raises(ValueError, match="stall escalation threshold"):
        PolicyRoutingKnobs(stall_escalation_threshold=threshold)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("fraction", [0.0, -0.5, 1.5, float("nan"), float("inf")])
def test_routing_knobs_reject_bad_budget_pressure_fraction(fraction: float) -> None:
    with pytest.raises(ValueError, match="budget pressure fraction"):
        PolicyRoutingKnobs(budget_pressure_remaining_fraction=fraction)


@pytest.mark.parametrize("fraction", [True, False])
def test_routing_knobs_reject_bool_budget_pressure_fraction(fraction: bool) -> None:
    # M9: JSON ``true``/``false`` must not pass as 1.0/0.0.
    with pytest.raises(ValueError, match="budget pressure fraction"):
        PolicyRoutingKnobs(budget_pressure_remaining_fraction=fraction)


# --- Context allocation bounds: allow + deny ----------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"floor_tokens": 0},
        {"floor_tokens": -1},
        {"floor_tokens": True},
        {"floor_tokens": 1.5},
        {"ceiling_tokens": 1023},
        {"reserve_tokens": -1},
        {"reserve_tokens": 1024},
        {"reserve_tokens": 2048},
        {"step_tokens": 0},
        {"step_tokens": -1},
        {"low_utilization_fraction": 0.0},
        {"low_utilization_fraction": 1.0},
        {"low_utilization_fraction": float("nan")},
    ],
)
def test_bounds_reject_invalid_shapes(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"tokens|fraction"):
        _bounds(**overrides)


def test_adjust_grows_toward_the_ceiling_on_over_budget_drops() -> None:
    bounds = _bounds(step_tokens=512)
    current = ContextTokenBudget(max_tokens=1024)
    grown = bounds.adjust(current, dropped_over_budget=True, utilization_fraction=0.9)
    assert grown.max_tokens == 1536


def test_adjust_growth_clamps_at_the_ceiling() -> None:
    bounds = _bounds(step_tokens=512)
    current = ContextTokenBudget(max_tokens=4096)
    saturated = bounds.adjust(current, dropped_over_budget=True, utilization_fraction=0.9)
    assert saturated.max_tokens == 4096


def test_adjust_shrinks_below_the_utilization_threshold() -> None:
    bounds = _bounds(step_tokens=512, low_utilization_fraction=0.5)
    current = ContextTokenBudget(max_tokens=4096)
    shrunk = bounds.adjust(current, dropped_over_budget=False, utilization_fraction=0.25)
    assert shrunk.max_tokens == 3584


def test_adjust_shrink_clamps_at_the_floor() -> None:
    bounds = _bounds(step_tokens=512)
    current = ContextTokenBudget(max_tokens=1024)
    floored = bounds.adjust(current, dropped_over_budget=False, utilization_fraction=0.1)
    assert floored.max_tokens == 1024


def test_adjust_retains_at_or_above_the_utilization_threshold() -> None:
    bounds = _bounds(step_tokens=512, low_utilization_fraction=0.5)
    current = ContextTokenBudget(max_tokens=2048)
    retained = bounds.adjust(current, dropped_over_budget=False, utilization_fraction=0.5)
    assert retained.max_tokens == 2048


def test_adjust_carries_the_policy_reserve() -> None:
    bounds = _bounds(reserve_tokens=128)
    adjusted = bounds.adjust(
        ContextTokenBudget(max_tokens=1024, reserve_tokens=64),
        dropped_over_budget=True,
        utilization_fraction=0.9,
    )
    assert adjusted.reserve_tokens == 128


@pytest.mark.parametrize("fraction", [-0.1, 1.1, float("nan"), float("inf")])
def test_adjust_rejects_out_of_range_utilization(fraction: float) -> None:
    with pytest.raises(ValueError, match="utilization fraction"):
        _bounds().adjust(
            ContextTokenBudget(max_tokens=1024),
            dropped_over_budget=False,
            utilization_fraction=fraction,
        )


def test_adjust_result_is_always_inside_the_bounds() -> None:
    bounds = _bounds(step_tokens=100)
    for start in (1, 1024, 2048, 4096, 99999):
        for dropped in (False, True):
            for utilization in (0.0, 0.49, 0.5, 1.0):
                result = bounds.adjust(
                    ContextTokenBudget(max_tokens=start),
                    dropped_over_budget=dropped,
                    utilization_fraction=utilization,
                )
                assert bounds.floor_tokens <= result.max_tokens <= bounds.ceiling_tokens


# --- Registry: allow + deny ----------------------------------------------------


def test_builtin_registry_contains_the_three_pinned_policies() -> None:
    assert builtin_policies() == (
        BASELINE_POLICY,
        ADAPTIVE_CONTEXT_POLICY,
        PATIENT_ROUTER_POLICY,
    )


def test_baseline_policy_mirrors_the_pre_pacs_017_wiring() -> None:
    assert BASELINE_POLICY.initial_context_budget() == ContextTokenBudget(
        max_tokens=4096, reserve_tokens=256
    )
    assert BASELINE_POLICY.verify_read_only_turns is False
    assert BASELINE_POLICY.worker_count is None
    assert BASELINE_POLICY.routing == PolicyRoutingKnobs()


def test_registry_versions_are_unique_per_policy_id() -> None:
    keys = [(policy.policy_id, policy.version) for policy in builtin_policies()]
    assert len(keys) == len(set(keys))


def test_resolve_policy_returns_the_registered_instance() -> None:
    assert resolve_policy("baseline") is BASELINE_POLICY
    assert resolve_policy("adaptive-context", version=1) is ADAPTIVE_CONTEXT_POLICY


def test_resolve_policy_without_version_prefers_the_highest() -> None:
    assert resolve_policy("patient-router").version == max(
        policy.version for policy in builtin_policies() if policy.policy_id == "patient-router"
    )


def test_resolve_policy_fails_closed_on_unknown_id() -> None:
    with pytest.raises(UnknownPolicyError, match="unknown policy"):
        resolve_policy("does-not-exist")


def test_resolve_policy_fails_closed_on_unknown_version() -> None:
    with pytest.raises(UnknownPolicyError, match="unknown version"):
        resolve_policy("baseline", version=99)


# --- Policy registry lifecycle vocabulary (PACS-017 M6) ----------------------


def _record(**overrides: object) -> PolicyRecord:
    base: dict[str, object] = {
        "policy": _policy(),
        "lifecycle": PolicyLifecycle.CANDIDATE,
        "evidence_basis": "registered by operator",
    }
    base.update(overrides)
    return PolicyRecord(**base)  # pyright: ignore[reportArgumentType]


def test_lifecycle_vocabulary_is_closed() -> None:
    assert {state.value for state in PolicyLifecycle} == {
        "candidate",
        "shadowed",
        "benchmarked",
        "promoted",
        "retired",
    }


def test_record_constructs_with_a_referenced_basis() -> None:
    record = _record(note="first cut")
    assert record.lifecycle is PolicyLifecycle.CANDIDATE
    assert record.evidence_basis == "registered by operator"
    assert record.note == "first cut"


def test_record_note_defaults_to_empty() -> None:
    assert _record().note == ""


@pytest.mark.parametrize("basis", ["", "   "])
def test_record_rejects_a_blank_evidence_basis(basis: str) -> None:
    with pytest.raises(ValueError, match="evidence_basis cannot be empty"):
        _record(evidence_basis=basis)


@pytest.mark.parametrize("field_name", ["evidence_basis", "note"])
def test_record_text_is_bounded(field_name: str) -> None:
    with pytest.raises(ValueError, match=rf"{field_name} must be at most {MAX_POLICY_TEXT}"):
        _record(**{field_name: "x" * (MAX_POLICY_TEXT + 1)})


@pytest.mark.parametrize("field_name", ["evidence_basis", "note"])
def test_record_text_rejects_control_characters(field_name: str) -> None:
    with pytest.raises(ValueError, match=rf"{field_name} must not contain control characters"):
        _record(**{field_name: "basis\nwith newline"})


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (PolicyLifecycle.CANDIDATE, PolicyLifecycle.SHADOWED),
        (PolicyLifecycle.CANDIDATE, PolicyLifecycle.BENCHMARKED),
        (PolicyLifecycle.CANDIDATE, PolicyLifecycle.RETIRED),
        (PolicyLifecycle.SHADOWED, PolicyLifecycle.BENCHMARKED),
        (PolicyLifecycle.SHADOWED, PolicyLifecycle.PROMOTED),
        (PolicyLifecycle.SHADOWED, PolicyLifecycle.RETIRED),
        (PolicyLifecycle.BENCHMARKED, PolicyLifecycle.PROMOTED),
        (PolicyLifecycle.BENCHMARKED, PolicyLifecycle.RETIRED),
    ],
)
def test_legal_transitions_move_the_record(
    source: PolicyLifecycle, target: PolicyLifecycle
) -> None:
    record = _record(lifecycle=source)
    updated = transition_policy_record(record, target, evidence_basis="eval-report-7")
    assert updated.lifecycle is target
    assert updated.evidence_basis == "eval-report-7"
    assert updated.policy == record.policy
    # Pure: the source record is untouched.
    assert record.lifecycle is source


@pytest.mark.parametrize(
    ("source", "target"),
    [
        # A fresh candidate can never jump straight to PROMOTED: promotion
        # always passes through an evidence-gathering state first (rule 16).
        (PolicyLifecycle.CANDIDATE, PolicyLifecycle.PROMOTED),
        # Terminal states never mutate: supersession is re-registration of
        # a NEW version, never a silent flip of a promoted/retired record.
        (PolicyLifecycle.PROMOTED, PolicyLifecycle.CANDIDATE),
        (PolicyLifecycle.PROMOTED, PolicyLifecycle.RETIRED),
        (PolicyLifecycle.RETIRED, PolicyLifecycle.CANDIDATE),
        (PolicyLifecycle.RETIRED, PolicyLifecycle.PROMOTED),
        # No backward or lateral moves outside the table.
        (PolicyLifecycle.SHADOWED, PolicyLifecycle.CANDIDATE),
        (PolicyLifecycle.BENCHMARKED, PolicyLifecycle.SHADOWED),
        (PolicyLifecycle.BENCHMARKED, PolicyLifecycle.CANDIDATE),
    ],
)
def test_illegal_transitions_fail_closed(source: PolicyLifecycle, target: PolicyLifecycle) -> None:
    with pytest.raises(ValueError, match=r"lifecycle transition .* is not legal"):
        transition_policy_record(_record(lifecycle=source), target, evidence_basis="eval-report-7")


def test_transition_requires_a_fresh_evidence_basis() -> None:
    with pytest.raises(ValueError, match="evidence_basis cannot be empty"):
        transition_policy_record(_record(), PolicyLifecycle.SHADOWED, evidence_basis="   ")


def test_transition_carries_the_note_unless_replaced() -> None:
    record = _record(note="keep me")
    carried = transition_policy_record(record, PolicyLifecycle.SHADOWED, evidence_basis="run-3")
    assert carried.note == "keep me"
    replaced = transition_policy_record(
        record, PolicyLifecycle.SHADOWED, evidence_basis="run-3", note="new note"
    )
    assert replaced.note == "new note"


def test_no_self_promotion_path_exists_in_the_domain() -> None:
    # Structural pin: a record can only reach PROMOTED through an explicit
    # operator transition call carrying a fresh evidence basis — the record
    # itself is frozen, so no in-place mutation path exists at all.
    record = _record(lifecycle=PolicyLifecycle.SHADOWED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.lifecycle = PolicyLifecycle.PROMOTED  # type: ignore[misc]
