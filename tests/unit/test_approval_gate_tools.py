"""ApprovalGateTools: operator-tightened authority metadata (PACS-014).

The wrapper upgrades named tools to ``ApprovalClass.REQUIRED`` at wiring
time; names that do not resolve against the delegate fail closed at
construction, and execution delegates unchanged.
"""

from __future__ import annotations

import pytest

from loopforge.adapters.approval_gate_tools import ApprovalGateTools
from loopforge.domain.actions import ActionProposal
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, Permission, RiskLevel
from loopforge.ports.tools import ToolExecutionRequest, ToolResult


def _metadata(name: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.LOCAL_WRITE,
        required_permission=Permission.LOCAL_WRITE,
        side_effect=SideEffectClass.LOCAL_WRITE,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NONE,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


class _FakeTools:
    """NamedToolExecutor-conformant fake over two registered tools."""

    def __init__(self) -> None:
        self._metadata = {
            "write_file": _metadata("write_file"),
            "read_file": _metadata("read_file"),
        }
        self.executed: list[str] = []

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._metadata)

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        return self._metadata[tool_name]

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        self.executed.append(request.proposal.tool_name)
        return ToolResult(ok=True, observation="done")


def _delegate() -> _FakeTools:
    return _FakeTools()


def test_gated_tools_require_approval_and_the_rest_are_untouched() -> None:
    tools = ApprovalGateTools(_delegate(), required_for=frozenset({"write_file"}))

    assert tools.metadata_for("write_file").approval is ApprovalClass.REQUIRED
    assert tools.metadata_for("read_file").approval is ApprovalClass.NONE
    # Only the approval class tightens; every other authority field delegates.
    assert tools.metadata_for("write_file").risk is RiskLevel.LOCAL_WRITE
    assert tools.tool_names == ("write_file", "read_file")


def test_execute_delegates_unchanged() -> None:
    tools = ApprovalGateTools(_delegate(), required_for=frozenset({"write_file"}))
    request = ToolExecutionRequest(
        proposal=ActionProposal(ActionId("a1"), "write_file", {"path": "x.py"}),
        attempt=1,
        timeout_seconds=5.0,
    )
    result = tools.execute(request)
    assert result.ok is True
    assert result.observation == "done"


def test_unknown_gated_tool_fails_closed_at_construction() -> None:
    with pytest.raises(ValueError, match="not registered"):
        ApprovalGateTools(_delegate(), required_for=frozenset({"deploy_prod"}))
