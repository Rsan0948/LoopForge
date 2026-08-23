from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum

from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import ToolMetadata
from loopforge.ports.tools import ToolExecutionRequest, ToolExecutorPort, ToolResult


class InjectedProcessCrash(RuntimeError):
    """Deterministic process-interruption surrogate used by resilience tests."""


class ToolFaultKind(StrEnum):
    """Faults that can be injected at a tool execution boundary."""

    TRANSIENT_TIMEOUT = "transient_timeout"
    AMBIGUOUS_AFTER_SUCCESS = "ambiguous_after_success"
    CRASH_AFTER_SUCCESS = "crash_after_success"


@dataclass(frozen=True, slots=True)
class ToolFault:
    kind: ToolFaultKind
    error_code: str | None = None
    message: str | None = None


class FaultInjectingTools:
    """Wrap a real/deterministic tool adapter with a per-tool deterministic fault script.

    The wrapper intentionally does not decide whether a fault is retryable. It reports an
    operational observation and leaves retry/idempotency policy to the runtime.
    """

    def __init__(
        self,
        delegate: ToolExecutorPort,
        *,
        faults: dict[str, list[ToolFault]],
    ) -> None:
        self._delegate = delegate
        self._faults = {name: deque(items) for name, items in faults.items()}
        self.invocations: dict[str, int] = defaultdict(int)

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        return self._delegate.metadata_for(tool_name)

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        tool_name = request.proposal.tool_name
        self.invocations[tool_name] += 1
        queue = self._faults.get(tool_name)
        fault = queue.popleft() if queue else None
        if fault is None:
            return self._delegate.execute(request)

        if fault.kind is ToolFaultKind.TRANSIENT_TIMEOUT:
            return ToolResult(
                ok=False,
                observation=fault.message or "injected timeout",
                error_code=fault.error_code or "TIMEOUT",
                failure_class=ToolFailureClass.TRANSIENT,
            )

        result = self._delegate.execute(request)
        if not result.ok:
            return result
        if fault.kind is ToolFaultKind.AMBIGUOUS_AFTER_SUCCESS:
            return ToolResult(
                ok=False,
                observation=fault.message or "response lost after remote success",
                error_code=fault.error_code or "AMBIGUOUS_RESPONSE_LOSS",
                failure_class=ToolFailureClass.AMBIGUOUS_OUTCOME,
            )
        if fault.kind is ToolFaultKind.CRASH_AFTER_SUCCESS:
            raise InjectedProcessCrash(fault.message or "injected crash after side effect")
        raise AssertionError(f"unsupported fault: {fault.kind}")
