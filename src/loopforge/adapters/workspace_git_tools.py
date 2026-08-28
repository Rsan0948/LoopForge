"""Workspace status/diff/revert tools exposed through the tool registry.

These tools surface the ``WorkspacePort`` Git primitives (status, diff,
checkout) to the model-facing tool loop. All Git semantics stay in the
adapter layer; tool metadata is code-owned (AGENTS.md rule 4) and workspace
operations remain confined to the assigned workspace.
"""

from __future__ import annotations

from typing import Final

from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import Permission, RiskLevel
from loopforge.ports.tools import ToolExecutionRequest, ToolResult, UnknownToolError
from loopforge.ports.workspace import WorkspaceError, WorkspacePort

_WORKSPACE_STATUS: Final = "workspace_status"
_WORKSPACE_DIFF: Final = "workspace_diff"
_REVERT_FILE: Final = "revert_file"


class WorkspaceGitTools:
    """Read-only workspace inspection plus revert through ``WorkspacePort``."""

    def __init__(self, workspace: WorkspacePort, *, max_diff_bytes: int = 1_000_000) -> None:
        if max_diff_bytes <= 0:
            msg = "max_diff_bytes must be positive"
            raise ValueError(msg)
        self._workspace = workspace
        self._max_diff_bytes = max_diff_bytes
        self._metadata = {
            _WORKSPACE_STATUS: self._tool_metadata(
                _WORKSPACE_STATUS,
                risk=RiskLevel.READ_ONLY,
                side_effect=SideEffectClass.READ_ONLY,
                retry=RetryClass.SAFE,
                idempotency=IdempotencyClass.NOT_APPLICABLE,
            ),
            _WORKSPACE_DIFF: self._tool_metadata(
                _WORKSPACE_DIFF,
                risk=RiskLevel.READ_ONLY,
                side_effect=SideEffectClass.READ_ONLY,
                retry=RetryClass.SAFE,
                idempotency=IdempotencyClass.NOT_APPLICABLE,
            ),
            _REVERT_FILE: self._tool_metadata(
                _REVERT_FILE,
                risk=RiskLevel.LOCAL_WRITE,
                side_effect=SideEffectClass.LOCAL_WRITE,
                # Reverting a path to the base revision is naturally
                # idempotent: repeating it converges to the same state.
                retry=RetryClass.SAFE,
                idempotency=IdempotencyClass.NATURAL,
            ),
        }

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._metadata)

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        try:
            return self._metadata[tool_name]
        except KeyError as exc:
            msg = f"unknown workspace tool: {tool_name}"
            raise UnknownToolError(msg) from exc

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        name = request.proposal.tool_name
        if name not in self._metadata:
            msg = f"unknown workspace tool: {name}"
            raise UnknownToolError(msg)
        try:
            if name == _WORKSPACE_STATUS:
                return self._status()
            if name == _WORKSPACE_DIFF:
                return self._diff()
            return self._revert(request)
        except WorkspaceError as exc:
            return ToolResult(
                ok=False,
                observation=str(exc),
                error_code="WORKSPACE_ERROR",
                failure_class=ToolFailureClass.PERMANENT,
            )

    def _status(self) -> ToolResult:
        status = self._workspace.status()
        lines = [
            f"workspace_id={self._workspace.workspace_id}",
            f"base_revision={self._workspace.base_revision}",
            f"clean={status.clean}",
            "changed: " + (", ".join(status.changed) if status.changed else "-"),
            "untracked: " + (", ".join(status.untracked) if status.untracked else "-"),
        ]
        return ToolResult(ok=True, observation="\n".join(lines))

    def _diff(self) -> ToolResult:
        patch = self._workspace.diff()
        if len(patch.encode("utf-8")) > self._max_diff_bytes:
            return ToolResult(
                ok=False,
                observation="workspace diff exceeds the tool byte limit",
                error_code="WORKSPACE_DIFF_TOO_LARGE",
                failure_class=ToolFailureClass.PERMANENT,
            )
        return ToolResult(ok=True, observation=patch or "workspace is clean")

    def _revert(self, request: ToolExecutionRequest) -> ToolResult:
        path = request.proposal.arguments.get("path")
        # Boundary validation is intentional: model output may violate the
        # Mapping[str, str] argument contract.
        if not isinstance(path, str) or not path.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
            return ToolResult(
                ok=False,
                observation="tool argument 'path' is required and must be a string",
                error_code="TOOL_ARGUMENTS",
                failure_class=ToolFailureClass.PERMANENT,
            )
        self._workspace.checkout((path,))
        return ToolResult(ok=True, observation=f"reverted {path} to the base revision")

    @staticmethod
    def _tool_metadata(
        name: str,
        *,
        risk: RiskLevel,
        side_effect: SideEffectClass,
        retry: RetryClass,
        idempotency: IdempotencyClass,
    ) -> ToolMetadata:
        required_permission = {
            RiskLevel.READ_ONLY: Permission.READ,
            RiskLevel.LOCAL_WRITE: Permission.LOCAL_WRITE,
        }[risk]
        return ToolMetadata(
            name=name,
            risk=risk,
            required_permission=required_permission,
            side_effect=side_effect,
            retry=retry,
            idempotency=idempotency,
            approval=ApprovalClass.NONE,
            timeout_seconds=10.0,
        )
