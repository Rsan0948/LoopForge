"""Composite tool executor dispatching across named executors.

The runtime binds a single ``ToolExecutorPort``; workloads compose several
executors (file tools, sandbox command tools, workspace Git tools) behind one
deterministic dispatch. Tool names must be unique across the composition —
duplicate registration fails at construction, before any execution.
"""

from __future__ import annotations

from typing import Protocol

from loopforge.domain.tooling import ToolMetadata
from loopforge.ports.tools import ToolExecutionRequest, ToolResult, UnknownToolError


class NamedToolExecutor(Protocol):
    """Structural contract for executors that can enumerate their tools."""

    @property
    def tool_names(self) -> tuple[str, ...]: ...

    def metadata_for(self, tool_name: str) -> ToolMetadata: ...

    def execute(self, request: ToolExecutionRequest) -> ToolResult: ...


class CompositeToolExecutor:
    """Dispatch tool calls to the executor that registered each tool name."""

    def __init__(self, executors: tuple[NamedToolExecutor, ...]) -> None:
        if not executors:
            msg = "composite tool executor requires at least one executor"
            raise ValueError(msg)
        self._dispatch: dict[str, NamedToolExecutor] = {}
        for executor in executors:
            for name in executor.tool_names:
                if name in self._dispatch:
                    msg_2 = f"tool name {name!r} is registered by more than one executor"
                    raise ValueError(msg_2)
                self._dispatch[name] = executor

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._dispatch)

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        return self._executor_for(tool_name).metadata_for(tool_name)

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        return self._executor_for(request.proposal.tool_name).execute(request)

    def _executor_for(self, tool_name: str) -> NamedToolExecutor:
        try:
            return self._dispatch[tool_name]
        except KeyError as exc:
            msg = f"unknown tool: {tool_name}"
            raise UnknownToolError(msg) from exc
