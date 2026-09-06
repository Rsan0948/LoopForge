"""Adaptive execution-policy vocabulary (PACS-017).

An ``ExecutionPolicy`` bundles exactly the knobs an adaptive system may
optimize — model selection, escalation timing, context allocation,
compaction pressure, verification cadence, and worker count — and nothing
else. The immutable surface (permissions, security boundaries, legal
transitions, hard budgets, HITL requirements, secret handling, and
authority-expansion rules) has no representation here: it cannot be
expressed, so no policy can widen it (AGENTS.md rules 11/12).

Policies are code-owned and versioned. Candidates are evaluated by the
locked benchmark suite (ADR-0012), shadowed as evidence-only records, and
promoted only by explicit operator action — a candidate can never
self-promote. "Evaluator selection" is deliberately absent: the independent
evaluator is deferred post-v1.0, so there is no honest knob to expose.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.routing import (
    ModelRequirements,
    ModelTier,
    RoutingPolicyConfig,
)

POLICY_ID_MAX_LENGTH: Final[int] = 64
_POLICY_ID_EXTRA_CHARS: Final[frozenset[str]] = frozenset("._-")


def _is_ascii_alnum(char: str) -> bool:
    return "a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9"


def is_valid_policy_id(policy_id: str) -> bool:
    """Safe policy identifier shape (mirrors the eval report-id rules)."""
    return (
        1 <= len(policy_id) <= POLICY_ID_MAX_LENGTH
        and _is_ascii_alnum(policy_id[0])
        and all(_is_ascii_alnum(char) or char in _POLICY_ID_EXTRA_CHARS for char in policy_id[1:])
    )


MAX_POLICY_WORKERS: Final[int] = 4
"""Code-owned ceiling for policy-selectable worker counts.

Mirrors the ``Orchestrator`` default ``max_workers``: a policy may choose
fewer workers, never more than the runtime's bounded multi-agent envelope.
"""


class UnknownPolicyError(ValueError):
    """Raised when a policy id/version cannot be resolved — fail closed."""


class ShadowDecisionKind(StrEnum):
    """Closed vocabulary of candidate-policy shadow decision kinds (PACS-017).

    Each kind names one adaptive-surface decision point where a shadowed
    candidate's choice is journaled as evidence-only
    ``ShadowDecisionRecorded`` events while the active policy executes.
    """

    MODEL_ROUTE = "model_route"
    CONTEXT_BUDGET = "context_budget"
    VERIFICATION_CADENCE = "verification_cadence"


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyRoutingKnobs:
    """Adaptive routing knobs: model-selection bias and escalation timing.

    Capability ``ModelRequirements`` are deliberately NOT here: they are a
    code-owned workload contract (mirroring ``SandboxRequirements``), not an
    adaptive knob. Wiring composes these knobs with the workload's
    requirements into a ``RoutingPolicyConfig``.
    """

    default_tier: ModelTier = ModelTier.STANDARD
    stall_escalation_threshold: int = 2
    budget_pressure_remaining_fraction: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.stall_escalation_threshold, int
        ) or isinstance(self.stall_escalation_threshold, bool):
            msg = "stall escalation threshold must be an integer"
            raise ValueError(msg)  # noqa: TRY004
        if self.stall_escalation_threshold < 1:
            msg_2 = "stall escalation threshold must be positive"
            raise ValueError(msg_2)
        if self.budget_pressure_remaining_fraction is not None:
            if not math.isfinite(self.budget_pressure_remaining_fraction):
                msg_3 = "budget pressure fraction must be finite"
                raise ValueError(msg_3)
            if not 0.0 < self.budget_pressure_remaining_fraction <= 1.0:
                msg_4 = "budget pressure fraction must be in (0, 1]"
                raise ValueError(msg_4)

    def for_requirements(self, requirements: ModelRequirements) -> RoutingPolicyConfig:
        """Compose these knobs with code-owned workload requirements."""
        return RoutingPolicyConfig(
            requirements=requirements,
            default_tier=self.default_tier,
            stall_escalation_threshold=self.stall_escalation_threshold,
            budget_pressure_remaining_fraction=self.budget_pressure_remaining_fraction,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextAllocationBounds:
    """Policy-owned bounds for adaptive per-turn context-budget allocation.

    The allocator may move the per-assembly budget only inside
    ``[floor_tokens, ceiling_tokens]``; enforcement of the resulting budget
    stays with ``select_context``/``ContextAccounting`` (an over-budget
    assembly is unrepresentable). ``low_utilization_fraction`` is the
    compaction-pressure threshold: below it the allocator contracts, and any
    over-budget drop makes it grow — both by ``step_tokens``.
    """

    floor_tokens: int
    ceiling_tokens: int
    reserve_tokens: int = 0
    step_tokens: int = 512
    low_utilization_fraction: float = 0.5

    def __post_init__(self) -> None:
        for name, value in (
            ("floor_tokens", self.floor_tokens),
            ("ceiling_tokens", self.ceiling_tokens),
            ("reserve_tokens", self.reserve_tokens),
            ("step_tokens", self.step_tokens),
        ):
            if not isinstance(value, int) or isinstance(value, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg = f"{name} must be an integer"
                raise ValueError(msg)  # noqa: TRY004
        if self.floor_tokens <= 0:
            msg_2 = "floor_tokens must be positive"
            raise ValueError(msg_2)
        if self.ceiling_tokens < self.floor_tokens:
            msg_3 = "ceiling_tokens cannot be below floor_tokens"
            raise ValueError(msg_3)
        if self.reserve_tokens < 0:
            msg_4 = "reserve_tokens cannot be negative"
            raise ValueError(msg_4)
        if self.reserve_tokens >= self.floor_tokens:
            msg_5 = "reserve_tokens must leave room for content at the floor budget"
            raise ValueError(msg_5)
        if self.step_tokens <= 0:
            msg_6 = "step_tokens must be positive"
            raise ValueError(msg_6)
        if not math.isfinite(self.low_utilization_fraction):
            msg_7 = "low utilization fraction must be finite"
            raise ValueError(msg_7)
        if not 0.0 < self.low_utilization_fraction < 1.0:
            msg_8 = "low utilization fraction must be in (0, 1)"
            raise ValueError(msg_8)

    def initial_budget(self) -> ContextTokenBudget:
        """The budget the first assembly starts from (the policy floor)."""
        return ContextTokenBudget(
            max_tokens=self.floor_tokens,
            reserve_tokens=self.reserve_tokens,
        )

    def adjust(
        self,
        current: ContextTokenBudget,
        *,
        dropped_over_budget: bool,
        utilization_fraction: float,
    ) -> ContextTokenBudget:
        """Next per-turn budget given the previous assembly's outcome.

        Pure and deterministic: grow toward the ceiling when the previous
        assembly dropped content over budget, contract toward the floor when
        utilization sits below the policy threshold, otherwise retain. The
        result is always clamped inside the policy bounds.
        """
        if not math.isfinite(utilization_fraction):
            msg = "utilization fraction must be finite"
            raise ValueError(msg)
        if not 0.0 <= utilization_fraction <= 1.0:
            msg_2 = "utilization fraction must be in [0, 1]"
            raise ValueError(msg_2)
        if dropped_over_budget:
            candidate = current.max_tokens + self.step_tokens
        elif utilization_fraction < self.low_utilization_fraction:
            candidate = current.max_tokens - self.step_tokens
        else:
            candidate = current.max_tokens
        clamped = min(max(candidate, self.floor_tokens), self.ceiling_tokens)
        return ContextTokenBudget(max_tokens=clamped, reserve_tokens=self.reserve_tokens)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionPolicy:
    """A code-owned, versioned bundle of adaptive-surface knobs.

    There are deliberately no fields for permissions, security boundaries,
    legal transitions, hard budgets, HITL requirements, secret handling, or
    authority-expansion rules: those stay immutable during runs (AGENTS.md
    rule 11). ``worker_count`` is a wiring-time preference only — budget
    partitioning and ``max_workers`` enforcement stay with the orchestrator.
    """

    policy_id: str
    version: int
    routing: PolicyRoutingKnobs = PolicyRoutingKnobs()
    context_allocation: ContextAllocationBounds
    verify_read_only_turns: bool = False
    worker_count: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not is_valid_policy_id(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.policy_id
        ):
            msg = "policy_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}"
            raise ValueError(msg)
        if not isinstance(self.version, int) or isinstance(self.version, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = "policy version must be an integer"
            raise ValueError(msg_2)  # noqa: TRY004
        if self.version < 1:
            msg_3 = "policy version must be positive"
            raise ValueError(msg_3)
        if self.worker_count is not None:
            if not isinstance(self.worker_count, int) or isinstance(self.worker_count, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg_4 = "worker_count must be an integer"
                raise ValueError(msg_4)
            if not 1 <= self.worker_count <= MAX_POLICY_WORKERS:
                msg_5 = f"worker_count must be in [1, {MAX_POLICY_WORKERS}]"
                raise ValueError(msg_5)

    def initial_context_budget(self) -> ContextTokenBudget:
        """The context budget a run under this policy starts from."""
        return self.context_allocation.initial_budget()


_FIXED_4096_BOUNDS: Final[ContextAllocationBounds] = ContextAllocationBounds(
    floor_tokens=4096,
    ceiling_tokens=4096,
    reserve_tokens=256,
)

BASELINE_POLICY: Final[ExecutionPolicy] = ExecutionPolicy(
    policy_id="baseline",
    version=1,
    context_allocation=_FIXED_4096_BOUNDS,
)
"""Mirrors the pre-PACS-017 wiring exactly: fixed 4096/256 context budget."""

ADAPTIVE_CONTEXT_POLICY: Final[ExecutionPolicy] = ExecutionPolicy(
    policy_id="adaptive-context",
    version=1,
    context_allocation=ContextAllocationBounds(
        floor_tokens=1024,
        ceiling_tokens=4096,
        reserve_tokens=256,
        step_tokens=512,
    ),
)
"""Candidate: adaptive context allocation between 1024 and 4096 tokens."""

PATIENT_ROUTER_POLICY: Final[ExecutionPolicy] = ExecutionPolicy(
    policy_id="patient-router",
    version=1,
    routing=PolicyRoutingKnobs(
        stall_escalation_threshold=3,
        budget_pressure_remaining_fraction=0.25,
    ),
    context_allocation=_FIXED_4096_BOUNDS,
)
"""Candidate: later stall escalation with budget-pressure de-escalation."""

BUILTIN_POLICIES: Final[tuple[ExecutionPolicy, ...]] = (
    BASELINE_POLICY,
    ADAPTIVE_CONTEXT_POLICY,
    PATIENT_ROUTER_POLICY,
)


def builtin_policies() -> tuple[ExecutionPolicy, ...]:
    """The code-owned registry of built-in policy versions."""
    return BUILTIN_POLICIES


def resolve_policy(policy_id: str, *, version: int | None = None) -> ExecutionPolicy:
    """Resolve a built-in policy by id (and optional version); fail closed.

    With ``version=None`` the highest registered version wins. Unknown ids
    and unknown versions both raise ``UnknownPolicyError`` — an
    unresolvable policy is never silently substituted.
    """
    matches = [policy for policy in BUILTIN_POLICIES if policy.policy_id == policy_id]
    if not matches:
        msg = f"unknown policy: {policy_id!r}"
        raise UnknownPolicyError(msg)
    if version is None:
        return max(matches, key=lambda policy: policy.version)
    for policy in matches:
        if policy.version == version:
            return policy
    msg_2 = f"unknown version {version} for policy {policy_id!r}"
    raise UnknownPolicyError(msg_2)
