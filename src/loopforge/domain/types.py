from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

RunId = NewType("RunId", str)
EventId = NewType("EventId", str)
ActionId = NewType("ActionId", str)
WorkerId = NewType("WorkerId", str)


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    ACTING = "acting"
    VERIFYING = "verifying"
    REFLECTING = "reflecting"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALLED = "stalled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.STALLED,
            self.BUDGET_EXHAUSTED,
            self.CANCELLED,
        }


class StopReason(StrEnum):
    SUCCESS_VERIFIED = "success_verified"
    FAILURE = "failure"
    STALLED = "stalled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MAX_ITERATIONS = "max_iterations"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    CRITICAL = "critical"


class Permission(StrEnum):
    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    CRITICAL = "critical"


class ControlDecisionKind(StrEnum):
    CONTINUE = "continue"
    STOP_SUCCESS = "stop_success"
    STOP_FAILURE = "stop_failure"
    STOP_BUDGET = "stop_budget"
    STOP_STALLED = "stop_stalled"
    REQUEST_HUMAN = "request_human"


@dataclass(frozen=True, slots=True)
class BudgetLimit:
    max_cost_usd: float
    max_iterations: int
    max_total_tokens: int | None = None
    max_elapsed_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive")
        if self.max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if self.max_total_tokens is not None and self.max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be positive when set")
        if self.max_elapsed_seconds is not None and self.max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive when set")


@dataclass(frozen=True, slots=True)
class UsageDelta:
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        if self.cost_usd < 0:
            msg = "cost_usd cannot be negative"
            raise ValueError(msg)
        if min(self.input_tokens, self.output_tokens, self.cached_input_tokens) < 0:
            msg = "token counts cannot be negative"
            raise ValueError(msg)
