"""Orchestrator/worker vocabulary (PACS-013).

Pure domain knowledge: closed worker/merge outcome vocabularies, the
spawn-time worker assignment contract, the replayable roster projection, and
fail-closed budget partitioning. The orchestrator owns the global plan;
workers own assigned workspaces and per-worker run streams. Budget
partitioning grants *shares* of the code-owned global limit — enforcement
stays entirely with ``ControlPolicy`` (AGENTS.md rule 12: adaptive structure
may never expand runtime authority).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from loopforge.domain.types import BudgetLimit, RunId, WorkerId, WorkspaceId

MAX_WORKER_TEXT_CHARS: Final[int] = 2_000
MAX_WORKER_ID_CHARS: Final[int] = 128


class WorkerOutcome(StrEnum):
    """Closed vocabulary of worker terminal outcomes (orchestrator stream)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"


class MergeOutcome(StrEnum):
    """Closed vocabulary of worker-branch reconciliation outcomes."""

    MERGED = "merged"
    CONFLICT = "conflict"


def _has_unsafe_characters(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def validate_worker_id(worker_id: str) -> None:
    """Worker ids double as branch/path components: plain names only."""
    if not worker_id.strip():
        msg = "worker_id cannot be empty"
        raise ValueError(msg)
    if any(char in worker_id for char in "/\\") or worker_id in {".", ".."}:
        msg_2 = "worker_id must be a plain name without path separators"
        raise ValueError(msg_2)
    if len(worker_id) > MAX_WORKER_ID_CHARS or _has_unsafe_characters(worker_id):
        msg_3 = "worker_id must not contain control characters or exceed 128 characters"
        raise ValueError(msg_3)


def validate_worker_text(text: str, field_name: str) -> None:
    """Bound free text before it enters a durable worker lifecycle event."""
    if not text.strip():
        msg = f"{field_name} cannot be empty"
        raise ValueError(msg)
    if len(text) > MAX_WORKER_TEXT_CHARS or _has_unsafe_characters(text):
        msg_2 = (
            f"{field_name} must not contain control characters or exceed "
            f"{MAX_WORKER_TEXT_CHARS} characters"
        )
        raise ValueError(msg_2)


def validate_budget_share(share_cost_usd: float) -> None:
    if not math.isfinite(share_cost_usd):
        msg = "worker budget share must be finite"
        raise ValueError(msg)
    if share_cost_usd <= 0:
        msg_2 = "worker budget share must be positive"
        raise ValueError(msg_2)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerSpec:
    """Spawn-time worker assignment (wiring authority, never model output).

    ``budget`` is this worker's static share of the orchestrated run's global
    ``BudgetLimit``; the worker's own ``ControlPolicy`` enforces it exactly
    like a single-runtime budget. Workers get shares, never new authority.
    """

    worker_id: WorkerId
    workspace_id: WorkspaceId
    objective: str
    budget: BudgetLimit

    def __post_init__(self) -> None:
        validate_worker_id(str(self.worker_id))
        validate_worker_id(str(self.workspace_id))
        validate_worker_text(self.objective, "worker objective")
        if not isinstance(self.budget, BudgetLimit):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_4 = "worker budget must be a BudgetLimit"
            raise TypeError(msg_4)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerProjection:
    """Replayable roster element projected from worker lifecycle events."""

    worker_id: WorkerId
    worker_run_id: RunId
    workspace_id: WorkspaceId
    budget_share_cost_usd: float
    outcome: WorkerOutcome | None = None
    merge_outcome: MergeOutcome | None = None


def partition_budget(limit: BudgetLimit, count: int) -> tuple[BudgetLimit, ...]:
    """Split a global budget into ``count`` static per-worker shares.

    Cost and token ceilings are additive across workers, so shares are divided
    and the result is validated to never sum above the global limit (float
    division is nudged downward when rounding would overshoot). Iteration and
    elapsed limits are per-stream concerns and are inherited by every worker.
    Enforcement stays with each worker's ``ControlPolicy`` — this helper only
    derives limits; it grants no authority (rule 12).
    """
    if not isinstance(limit, BudgetLimit):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = "global budget must be a BudgetLimit"
        raise TypeError(msg)
    if not isinstance(count, int) or isinstance(count, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg_2 = "worker count must be an integer"
        raise ValueError(msg_2)  # noqa: TRY004
    if count < 1:
        msg_3 = "worker count must be at least one"
        raise ValueError(msg_3)
    cost_share = limit.max_cost_usd / count
    if cost_share * count > limit.max_cost_usd:
        # Float rounding must never let the shares sum above the global limit.
        cost_share = math.nextafter(cost_share, 0.0)
    if cost_share <= 0:
        msg_4 = "worker count is too large for the global cost budget"
        raise ValueError(msg_4)
    token_share = None
    if limit.max_total_tokens is not None:
        token_share = limit.max_total_tokens // count
        if token_share < 1:
            msg_5 = "worker count is too large for the global token budget"
            raise ValueError(msg_5)
    return tuple(
        BudgetLimit(
            max_cost_usd=cost_share,
            max_iterations=limit.max_iterations,
            max_total_tokens=token_share,
            max_elapsed_seconds=limit.max_elapsed_seconds,
        )
        for _ in range(count)
    )
