"""Approval-gating tool executor decorator (PACS-014).

Production tool adapters honestly classify their own authority metadata
(``approval=ApprovalClass.NONE`` for the local repair stack). The operator
— never the model, never repository content — may tighten that baseline at
wiring time by gating named tools behind a durable approval: the runtime
then quiesces on ``ApprovalRequested`` until an operator grants or rejects
the exact pending action. A grant can never expand authority — permissions
are re-authorized on the approved path.
"""

from __future__ import annotations

from dataclasses import replace

from loopforge.adapters.composite_tools import NamedToolExecutor
from loopforge.domain.tooling import ApprovalClass, ToolMetadata
from loopforge.ports.tools import ToolExecutionRequest, ToolResult


class ApprovalGateTools:
    """Wrap an executor, upgrading named tools to ``ApprovalClass.REQUIRED``.

    The gated set is operator authority decided at composition time: every
    name is validated against the delegate's registered tools at
    construction — an unknown name fails closed before any run starts.
    Execution delegates unchanged; only the authority metadata tightens.
    """

    def __init__(self, delegate: NamedToolExecutor, *, required_for: frozenset[str]) -> None:
        unknown = sorted(required_for - set(delegate.tool_names))
        if unknown:
            msg = f"approval-gated tools are not registered: {unknown}"
            raise ValueError(msg)
        self._delegate = delegate
        self._required_for = frozenset(required_for)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self._delegate.tool_names

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        metadata = self._delegate.metadata_for(tool_name)
        if tool_name not in self._required_for:
            return metadata
        return replace(metadata, approval=ApprovalClass.REQUIRED)

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        return self._delegate.execute(request)
