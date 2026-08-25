from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from loopforge.domain.state import RunState
from loopforge.domain.tooling import ToolMetadata
from loopforge.domain.types import BudgetLimit, ControlDecisionKind, Permission, StopReason


@dataclass(frozen=True, slots=True)
class ControlDecision:
    kind: ControlDecisionKind
    reason_code: str
    stop_reason: StopReason | None = None


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    granted: frozenset[Permission]

    def authorizes(self, metadata: ToolMetadata) -> bool:
        return metadata.required_permission in self.granted


@dataclass(frozen=True, slots=True)
class ControlPolicy:
    budget: BudgetLimit
    no_progress_limit: int = 3

    def __post_init__(self) -> None:
        if self.no_progress_limit <= 0:
            msg = "no_progress_limit must be positive"
            raise ValueError(msg)

    def evaluate(  # noqa: PLR0911 - flat prioritized rule chain keeps stop precedence explicit
        self, state: RunState, *, now: datetime | None = None
    ) -> ControlDecision:
        if state.last_verification_passed is True:
            return ControlDecision(
                ControlDecisionKind.STOP_SUCCESS,
                "STOP_SUCCESS_VERIFIED",
                StopReason.SUCCESS_VERIFIED,
            )
        if state.cost_usd >= self.budget.max_cost_usd:
            return ControlDecision(
                ControlDecisionKind.STOP_BUDGET,
                "STOP_BUDGET_EXHAUSTED",
                StopReason.BUDGET_EXHAUSTED,
            )
        if (
            self.budget.max_total_tokens is not None
            and state.total_tokens >= self.budget.max_total_tokens
        ):
            return ControlDecision(
                ControlDecisionKind.STOP_BUDGET,
                "STOP_TOKEN_BUDGET_EXHAUSTED",
                StopReason.BUDGET_EXHAUSTED,
            )
        if (
            self.budget.max_elapsed_seconds is not None
            and state.started_at is not None
            and now is not None
            and (now - state.started_at).total_seconds() >= self.budget.max_elapsed_seconds
        ):
            return ControlDecision(
                ControlDecisionKind.STOP_BUDGET,
                "STOP_TIME_BUDGET_EXHAUSTED",
                StopReason.BUDGET_EXHAUSTED,
            )
        if state.consecutive_no_progress >= self.no_progress_limit:
            return ControlDecision(
                ControlDecisionKind.STOP_STALLED,
                "STOP_STALLED_NO_PROGRESS",
                StopReason.STALLED,
            )
        if state.iteration >= self.budget.max_iterations:
            return ControlDecision(
                ControlDecisionKind.STOP_FAILURE,
                "STOP_MAX_ITERATIONS",
                StopReason.MAX_ITERATIONS,
            )
        return ControlDecision(ControlDecisionKind.CONTINUE, "CONTINUE_WITHIN_POLICY")
