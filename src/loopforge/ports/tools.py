from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.actions import ActionProposal
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import ToolMetadata


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    proposal: ActionProposal
    attempt: int
    timeout_seconds: float
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.attempt <= 0:
            msg = "attempt must be positive"
            raise ValueError(msg)
        if not math.isfinite(self.timeout_seconds):
            msg_5 = "timeout_seconds must be finite"
            raise ValueError(msg_5)
        if self.timeout_seconds <= 0:
            msg_2 = "timeout_seconds must be positive"
            raise ValueError(msg_2)


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    observation: str
    error_code: str | None = None
    failure_class: ToolFailureClass | None = None

    def __post_init__(self) -> None:
        if self.ok and self.failure_class is not None:
            msg_3 = "successful tool result cannot declare failure_class"
            raise ValueError(msg_3)
        if not self.ok and self.failure_class is None:
            msg_4 = "failed tool result requires failure_class"
            raise ValueError(msg_4)


class ToolContractError(TypeError):
    """Raised when a tool adapter violates the runtime response contract."""


class UnknownToolError(LookupError):
    """Raised when a proposal references a tool not registered in the runtime."""


class ToolExecutorPort(Protocol):
    def metadata_for(self, tool_name: str) -> ToolMetadata: ...

    def execute(self, request: ToolExecutionRequest) -> ToolResult: ...
