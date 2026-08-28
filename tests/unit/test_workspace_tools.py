"""Unit tests for workspace Git tools and the composite tool executor."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from loopforge.adapters.composite_tools import CompositeToolExecutor
from loopforge.adapters.file_tools import WorkspaceFileTools
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.local_sandbox import ConstrainedLocalSandbox
from loopforge.adapters.sandbox_tools import SandboxCommandTools, SandboxToolBinding
from loopforge.adapters.workspace_git_tools import WorkspaceGitTools
from loopforge.domain.actions import ActionProposal
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, Permission, RiskLevel
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.ports.tools import ToolExecutionRequest, UnknownToolError
from loopforge.ports.workspace import WorkspacePort

_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git executable unavailable; workspace tool tests require the Git CLI",
)

pytestmark = _REQUIRES_GIT


def _fixture() -> FixtureSpec:
    return FixtureSpec(
        fixture_id="sample",
        files=(FixtureFile(path="module.py", content="value = 1\n"),),
    )


def _workspace(tmp_path: Path) -> WorkspacePort:
    return GitWorkspaceManager(tmp_path / "workspaces").materialize(_fixture())


def _request(tool: str, **arguments: str) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        proposal=ActionProposal(ActionId("a1"), tool, arguments),
        attempt=1,
        timeout_seconds=5.0,
    )


def _metadata(name: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def test_workspace_status_tool_reports_clean_and_changed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    tools = WorkspaceGitTools(workspace)

    clean = tools.execute(_request("workspace_status"))
    assert clean.ok
    assert "clean=True" in clean.observation
    assert f"base_revision={workspace.base_revision}" in clean.observation

    (workspace.root / "module.py").write_text("value = 2\n", encoding="utf-8")
    changed = tools.execute(_request("workspace_status"))
    assert "clean=False" in changed.observation
    assert "changed: module.py" in changed.observation


def test_workspace_diff_tool_returns_exact_patch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    tools = WorkspaceGitTools(workspace)

    clean = tools.execute(_request("workspace_diff"))
    assert clean.ok
    assert clean.observation == "workspace is clean"

    (workspace.root / "module.py").write_text("value = 2\n", encoding="utf-8")
    patched = tools.execute(_request("workspace_diff"))
    assert "-value = 1" in patched.observation
    assert "+value = 2" in patched.observation


def test_workspace_diff_tool_enforces_byte_limit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    tools = WorkspaceGitTools(workspace, max_diff_bytes=16)
    (workspace.root / "module.py").write_text("value = 2\n", encoding="utf-8")

    result = tools.execute(_request("workspace_diff"))

    assert not result.ok
    assert result.error_code == "WORKSPACE_DIFF_TOO_LARGE"


def test_workspace_git_tools_validate_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_diff_bytes must be positive"):
        WorkspaceGitTools(_workspace(tmp_path), max_diff_bytes=0)


def test_revert_file_restores_base_and_requires_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    tools = WorkspaceGitTools(workspace)
    target = workspace.root / "module.py"
    target.write_text("value = 99\n", encoding="utf-8")

    reverted = tools.execute(_request("revert_file", path="module.py"))

    assert reverted.ok
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    missing = tools.execute(_request("revert_file"))
    assert not missing.ok
    assert missing.error_code == "TOOL_ARGUMENTS"
    unknown = tools.execute(_request("revert_file", path="never-committed.py"))
    assert not unknown.ok
    assert unknown.error_code == "WORKSPACE_ERROR"


def test_workspace_git_tools_unknown_tool_raises(tmp_path: Path) -> None:
    tools = WorkspaceGitTools(_workspace(tmp_path))
    with pytest.raises(UnknownToolError, match="unknown workspace tool"):
        tools.metadata_for("nope")
    with pytest.raises(UnknownToolError, match="unknown workspace tool"):
        tools.execute(_request("nope"))


def test_sandbox_command_tools_expose_tool_names(tmp_path: Path) -> None:
    sandbox = ConstrainedLocalSandbox(tmp_path, commands=[], environment={})
    tools = SandboxCommandTools(
        sandbox,
        [
            SandboxToolBinding(metadata=_metadata("run_tests"), command_name="run_tests"),
            SandboxToolBinding(metadata=_metadata("build"), command_name="build"),
        ],
    )
    assert tools.tool_names == ("run_tests", "build")


def test_composite_dispatches_by_tool_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    sandbox = ConstrainedLocalSandbox(workspace.root, commands=[], environment={})
    composite = CompositeToolExecutor(
        (
            WorkspaceFileTools(sandbox, workspace.root),
            WorkspaceGitTools(workspace),
        )
    )

    assert set(composite.tool_names) == {
        "read_file",
        "search_files",
        "write_file",
        "edit_file",
        "workspace_status",
        "workspace_diff",
        "revert_file",
    }
    assert composite.metadata_for("read_file").name == "read_file"
    assert composite.metadata_for("workspace_status").name == "workspace_status"
    result = composite.execute(_request("read_file", path="module.py"))
    assert result.ok
    assert result.observation == "value = 1\n"
    with pytest.raises(UnknownToolError, match="unknown tool"):
        composite.metadata_for("nope")
    with pytest.raises(UnknownToolError, match="unknown tool"):
        composite.execute(_request("nope"))


def test_composite_rejects_duplicate_names_and_empty_composition(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    sandbox = ConstrainedLocalSandbox(workspace.root, commands=[], environment={})
    file_tools = WorkspaceFileTools(sandbox, workspace.root)

    with pytest.raises(ValueError, match="more than one executor"):
        CompositeToolExecutor((file_tools, file_tools))
    with pytest.raises(ValueError, match="at least one executor"):
        CompositeToolExecutor(())


# --- PACS-010 hardening edge pins ---


def test_revert_rejects_non_string_path(tmp_path: Path) -> None:
    tools = WorkspaceGitTools(_workspace(tmp_path))
    proposal = ActionProposal(ActionId("a1"), "revert_file", {"path": 42})  # pyright: ignore[reportArgumentType]

    result = tools.execute(ToolExecutionRequest(proposal=proposal, attempt=1, timeout_seconds=5.0))

    assert not result.ok
    assert result.error_code == "TOOL_ARGUMENTS"


def test_revert_rejects_glob_and_git_metadata_paths(tmp_path: Path) -> None:
    tools = WorkspaceGitTools(_workspace(tmp_path))

    for bad in ("*.py", ":(top)module.py", ".git/config", "a\nb.py"):
        result = tools.execute(_request("revert_file", path=bad))
        assert not result.ok, bad
        assert result.error_code == "WORKSPACE_ERROR"
