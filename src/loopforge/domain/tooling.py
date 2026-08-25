from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from loopforge.domain.types import Permission, RiskLevel


class SideEffectClass(StrEnum):
    """How a tool can affect state outside the model runtime."""

    PURE = "pure"
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    IRREVERSIBLE = "irreversible"


class RetryClass(StrEnum):
    """Whether and under what conditions an operation may be retried."""

    NEVER = "never"
    TRANSIENT_ONLY = "transient_only"
    SAFE = "safe"


class IdempotencyClass(StrEnum):
    """The guarantees available when the same logical action is repeated."""

    NOT_APPLICABLE = "not_applicable"
    NATURAL = "natural"
    KEYED = "keyed"
    NONE = "none"


class ApprovalClass(StrEnum):
    """Human-approval requirement for a tool invocation."""

    NONE = "none"
    POLICY_DEPENDENT = "policy_dependent"
    REQUIRED = "required"


class DataSensitivity(StrEnum):
    """Highest expected sensitivity of data handled by the tool."""

    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolMetadata:
    """Static, code-owned contract for a tool capability.

    Model output may select a registered tool, but cannot alter these semantics.
    """

    name: str
    risk: RiskLevel
    required_permission: Permission
    side_effect: SideEffectClass
    retry: RetryClass
    idempotency: IdempotencyClass
    approval: ApprovalClass
    timeout_seconds: float
    sensitivity: DataSensitivity = DataSensitivity.INTERNAL

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "tool metadata name cannot be empty"
            raise ValueError(msg)
        if not math.isfinite(self.timeout_seconds):
            msg_2 = "timeout_seconds must be finite"
            raise ValueError(msg_2)
        if self.timeout_seconds <= 0:
            msg = "timeout_seconds must be positive"
            raise ValueError(msg)

        expected_permission = {
            RiskLevel.READ_ONLY: Permission.READ,
            RiskLevel.LOCAL_WRITE: Permission.LOCAL_WRITE,
            RiskLevel.EXTERNAL_WRITE: Permission.EXTERNAL_WRITE,
            RiskLevel.CRITICAL: Permission.CRITICAL,
        }[self.risk]
        if self.required_permission is not expected_permission:
            msg = (
                f"risk {self.risk.value} requires permission "
                f"{expected_permission.value}, got {self.required_permission.value}"
            )
            raise ValueError(msg)

        if (
            self.side_effect in {SideEffectClass.PURE, SideEffectClass.READ_ONLY}
            and self.idempotency is IdempotencyClass.NONE
        ):
            msg = "pure/read-only tools cannot declare idempotency=none"
            raise ValueError(msg)

        if (
            self.retry is not RetryClass.NEVER
            and self.side_effect
            in {
                SideEffectClass.LOCAL_WRITE,
                SideEffectClass.EXTERNAL_WRITE,
                SideEffectClass.IRREVERSIBLE,
            }
            and self.idempotency not in {IdempotencyClass.NATURAL, IdempotencyClass.KEYED}
        ):
            msg = "retryable side-effecting tools require natural or keyed idempotency"
            raise ValueError(msg)

        if (
            self.side_effect is SideEffectClass.IRREVERSIBLE
            and self.approval is not ApprovalClass.REQUIRED
        ):
            msg = "irreversible tools require human approval"
            raise ValueError(msg)
