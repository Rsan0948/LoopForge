from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from loopforge.domain.tooling import IdempotencyClass, RetryClass, ToolMetadata
from loopforge.domain.types import ActionId, RunId


class ToolFailureClass(StrEnum):
    """Operational classification of an observed tool failure."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    AMBIGUOUS_OUTCOME = "ambiguous_outcome"


@dataclass(frozen=True, slots=True)
class RetrySettings:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 4.0
    jitter_fraction: float = 0.20

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            msg = "max_attempts must be positive"
            raise ValueError(msg)
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            msg_2 = "retry delays cannot be negative"
            raise ValueError(msg_2)
        if self.max_delay_seconds < self.base_delay_seconds:
            msg_3 = "max_delay_seconds cannot be below base_delay_seconds"
            raise ValueError(msg_3)
        if not 0 <= self.jitter_fraction <= 1:
            msg_4 = "jitter_fraction must be between 0 and 1"
            raise ValueError(msg_4)


@dataclass(frozen=True, slots=True)
class RetryDecision:
    should_retry: bool
    reason_code: str
    next_attempt: int | None = None
    delay_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ReliabilityPolicy:
    retry: RetrySettings = RetrySettings()
    circuit_failure_threshold: int = 3

    def __post_init__(self) -> None:
        if self.circuit_failure_threshold <= 0:
            msg_5 = "circuit_failure_threshold must be positive"
            raise ValueError(msg_5)

    def retry_decision(
        self,
        *,
        metadata: ToolMetadata,
        failure_class: ToolFailureClass,
        attempt: int,
        action_id: ActionId,
    ) -> RetryDecision:
        if attempt >= self.retry.max_attempts:
            return RetryDecision(False, "RETRY_ATTEMPTS_EXHAUSTED")
        if metadata.retry is RetryClass.NEVER:
            return RetryDecision(False, "RETRY_TOOL_POLICY_NEVER")
        if failure_class is ToolFailureClass.PERMANENT:
            return RetryDecision(False, "RETRY_FAILURE_PERMANENT")
        if failure_class is ToolFailureClass.AMBIGUOUS_OUTCOME and metadata.idempotency not in {
            IdempotencyClass.NATURAL,
            IdempotencyClass.KEYED,
        }:
            return RetryDecision(False, "RETRY_AMBIGUOUS_NOT_IDEMPOTENT")

        next_attempt = attempt + 1
        delay = self._delay(action_id=action_id, next_attempt=next_attempt)
        return RetryDecision(
            True,
            "RETRY_TRANSIENT_FAILURE"
            if failure_class is ToolFailureClass.TRANSIENT
            else "RETRY_AMBIGUOUS_IDEMPOTENT",
            next_attempt=next_attempt,
            delay_seconds=delay,
        )

    def circuit_is_open(self, *, consecutive_failures: int) -> bool:
        return consecutive_failures >= self.circuit_failure_threshold

    def _delay(self, *, action_id: ActionId, next_attempt: int) -> float:
        raw = self.retry.base_delay_seconds * (2 ** max(0, next_attempt - 2))
        capped = min(raw, self.retry.max_delay_seconds)
        if capped == 0 or self.retry.jitter_fraction == 0:
            return capped
        seed_text = f"{action_id}:{next_attempt}"
        seed = sum((index + 1) * ord(char) for index, char in enumerate(seed_text))
        unit = (seed % 10_000) / 9_999
        signed = (unit * 2.0) - 1.0
        jitter = capped * self.retry.jitter_fraction * signed
        return max(0.0, capped + jitter)


def idempotency_key_for(metadata: ToolMetadata, run_id: RunId, action_id: ActionId) -> str | None:
    if metadata.idempotency is IdempotencyClass.KEYED:
        return f"loopforge:{run_id}:{action_id}"
    return None
