"""Unit tests for the safe workspace file tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopforge.adapters.file_tools import FileToolLimits, WorkspaceFileTools
from loopforge.adapters.local_sandbox import ConstrainedLocalSandbox
from loopforge.domain.actions import ActionProposal
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import IdempotencyClass, RetryClass, SideEffectClass
from loopforge.domain.types import ActionId
from loopforge.ports.tools import ToolExecutionRequest, UnknownToolError


def _sandbox(root: Path) -> ConstrainedLocalSandbox:
    return ConstrainedLocalSandbox(root, commands=[], environment={})


def _tools(root: Path, *, limits: FileToolLimits | None = None) -> WorkspaceFileTools:
    return WorkspaceFileTools(_sandbox(root), root, limits=limits)


def _request(tool: str, **arguments: str) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        proposal=ActionProposal(ActionId("a1"), tool, arguments),
        attempt=1,
        timeout_seconds=5.0,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello world\n", encoding="utf-8")
    return tmp_path


def test_tool_names_and_metadata_are_code_owned(workspace: Path) -> None:
    tools = _tools(workspace)
    assert set(tools.tool_names) == {"read_file", "search_files", "write_file", "edit_file"}

    read = tools.metadata_for("read_file")
    assert read.side_effect is SideEffectClass.READ_ONLY
    assert read.idempotency is IdempotencyClass.NOT_APPLICABLE

    write = tools.metadata_for("write_file")
    assert write.side_effect is SideEffectClass.LOCAL_WRITE
    assert write.idempotency is IdempotencyClass.NATURAL
    assert write.retry is RetryClass.SAFE

    edit = tools.metadata_for("edit_file")
    # Failed edits are ambiguous on replay, so editing fails closed instead of
    # claiming natural idempotency (AGENTS.md rule 5).
    assert edit.retry is RetryClass.NEVER
    assert edit.idempotency is IdempotencyClass.NONE


def test_unknown_tool_raises(workspace: Path) -> None:
    tools = _tools(workspace)
    with pytest.raises(UnknownToolError, match="unknown file tool"):
        tools.metadata_for("nope")
    with pytest.raises(UnknownToolError, match="unknown file tool"):
        tools.execute(_request("nope"))


def test_tools_root_must_exist(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(ValueError, match="file tools root must be an existing directory"):
        WorkspaceFileTools(sandbox, tmp_path / "missing")


def test_read_file_returns_content(workspace: Path) -> None:
    result = _tools(workspace).execute(_request("read_file", path="src/app.py"))
    assert result.ok
    assert result.observation == "def run():\n    return 1\n"


def test_read_file_rejects_traversal_and_missing(workspace: Path) -> None:
    tools = _tools(workspace)
    for path in ("../outside.py", "/etc/passwd", "missing.py"):
        result = tools.execute(_request("read_file", path=path))
        assert not result.ok
        assert result.failure_class is ToolFailureClass.PERMANENT
        assert result.error_code == "SANDBOX_POLICY"


def test_missing_arguments_fail_permanently(workspace: Path) -> None:
    tools = _tools(workspace)
    for tool, arguments in (
        ("read_file", {}),
        ("search_files", {}),
        ("write_file", {"path": "a.py"}),
        ("edit_file", {"path": "a.py", "old": "x"}),
    ):
        result = tools.execute(_request(tool, **arguments))
        assert not result.ok
        assert result.error_code == "TOOL_ARGUMENTS"
        assert result.failure_class is ToolFailureClass.PERMANENT


def test_search_files_finds_literal_matches_in_order(workspace: Path) -> None:
    (workspace / "src" / "other.py").write_text("run = 2\nrun += 1\n", encoding="utf-8")

    result = _tools(workspace).execute(_request("search_files", query="run"))

    assert result.ok
    lines = result.observation.splitlines()
    assert "src/app.py:1: def run():" in lines
    assert "src/other.py:1: run = 2" in lines
    assert "src/other.py:2: run += 1" in lines
    assert lines.index("src/app.py:1: def run():") < lines.index("src/other.py:1: run = 2")


def test_search_files_no_match(workspace: Path) -> None:
    result = _tools(workspace).execute(_request("search_files", query="zzz-absent"))
    assert result.ok
    assert result.observation == "no matches for 'zzz-absent'"


def test_search_files_skips_git_dir_and_symlinks(workspace: Path) -> None:
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("run = true\n", encoding="utf-8")
    outside_dir = workspace.parent / f"{workspace.name}-outside"
    outside_dir.mkdir()
    (outside_dir / "secret.py").write_text("run = 'secret'\n", encoding="utf-8")
    (workspace / "link.py").symlink_to(outside_dir / "secret.py")
    (workspace / "linked").symlink_to(outside_dir, target_is_directory=True)

    result = _tools(workspace).execute(_request("search_files", query="run"))

    assert result.ok
    assert ".git" not in result.observation
    assert "secret" not in result.observation
    assert "linked" not in result.observation


def test_search_files_enforces_limits(workspace: Path) -> None:
    for index in range(10):
        (workspace / f"f{index}.py").write_text("needle\n", encoding="utf-8")
    tools = _tools(workspace, limits=FileToolLimits(max_results=3))

    result = tools.execute(_request("search_files", query="needle"))

    assert result.ok
    assert "search truncated after 3 results" in result.observation
    limited = _tools(workspace, limits=FileToolLimits(max_query_bytes=2))
    rejected = limited.execute(_request("search_files", query="needle"))
    assert not rejected.ok
    assert rejected.error_code == "TOOL_ARGUMENTS"


def test_search_files_skips_oversized_files(workspace: Path) -> None:
    (workspace / "huge.py").write_text("needle\n" + "x" * 2048, encoding="utf-8")
    tools = _tools(workspace, limits=FileToolLimits(max_file_bytes=64))

    result = tools.execute(_request("search_files", query="needle"))

    assert result.ok
    assert "huge.py" not in result.observation


def test_write_file_round_trips_through_sandbox(workspace: Path) -> None:
    tools = _tools(workspace)

    written = tools.execute(_request("write_file", path="src/new.py", content="x = 3\n"))
    assert written.ok

    read = tools.execute(_request("read_file", path="src/new.py"))
    assert read.observation == "x = 3\n"


def test_write_file_rejects_traversal(workspace: Path) -> None:
    result = _tools(workspace).execute(
        _request("write_file", path="../escape.py", content="x = 1\n")
    )
    assert not result.ok
    assert result.error_code == "SANDBOX_POLICY"
    assert not (workspace.parent / "escape.py").exists()


def test_edit_file_replaces_unique_occurrence(workspace: Path) -> None:
    tools = _tools(workspace)

    edited = tools.execute(_request("edit_file", path="src/app.py", old="return 1", new="return 2"))

    assert edited.ok
    assert (workspace / "src" / "app.py").read_text(
        encoding="utf-8"
    ) == "def run():\n    return 2\n"


def test_edit_file_requires_exactly_one_occurrence(workspace: Path) -> None:
    tools = _tools(workspace)

    missing = tools.execute(_request("edit_file", path="src/app.py", old="nope", new="x"))
    assert not missing.ok
    assert "edit target not found" in missing.observation

    (workspace / "src" / "dup.py").write_text("a = 1\na = 1\n", encoding="utf-8")
    ambiguous = tools.execute(_request("edit_file", path="src/dup.py", old="a = 1", new="b = 2"))
    assert not ambiguous.ok
    assert "edit target is not unique" in ambiguous.observation
    # Neither failure mode may partially apply the edit.
    assert (workspace / "src" / "dup.py").read_text(encoding="utf-8") == "a = 1\na = 1\n"


def test_file_tool_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="file tool limits must be positive"):
        FileToolLimits(max_results=0)


# --- PACS-010 hardening: error-containment and fidelity edge pins ---


def test_read_file_on_binary_file_fails_as_tool_result_not_crash(workspace: Path) -> None:
    (workspace / "blob.bin").write_bytes(b"\x00\x01\xff\xfe")

    result = _tools(workspace).execute(_request("read_file", path="blob.bin"))

    assert not result.ok
    assert result.error_code == "SANDBOX_POLICY"
    assert "not valid utf-8" in result.observation


def test_read_file_on_unreadable_file_fails_as_tool_result_not_crash(workspace: Path) -> None:
    secret = workspace / "locked.txt"
    secret.write_text("hidden\n", encoding="utf-8")
    secret.chmod(0o000)
    try:
        result = _tools(workspace).execute(_request("read_file", path="locked.txt"))
    finally:
        secret.chmod(0o644)

    assert not result.ok
    assert result.error_code == "SANDBOX_POLICY"


def test_search_files_skips_unreadable_files_with_an_honest_note(workspace: Path) -> None:
    locked = workspace / "src" / "locked.py"
    locked.write_text("hello hidden\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        result = _tools(workspace).execute(_request("search_files", query="hello"))
    finally:
        locked.chmod(0o644)

    assert result.ok
    assert "README.md:1" in result.observation
    assert "skipped 1 unreadable file(s)" in result.observation


def test_non_string_argument_values_fail_as_tool_arguments(workspace: Path) -> None:
    tools = _tools(workspace)
    proposals = [
        ActionProposal(ActionId("a1"), "read_file", {"path": 123}),  # pyright: ignore[reportArgumentType]
        ActionProposal(ActionId("a1"), "write_file", {"path": ["x"], "content": "y"}),  # pyright: ignore[reportArgumentType]
        ActionProposal(ActionId("a1"), "edit_file", {"path": "a", "old": None, "new": "b"}),  # pyright: ignore[reportArgumentType]
    ]
    for proposal in proposals:
        result = tools.execute(
            ToolExecutionRequest(proposal=proposal, attempt=1, timeout_seconds=5.0)
        )
        assert not result.ok
        assert result.error_code == "TOOL_ARGUMENTS"
        assert result.failure_class is ToolFailureClass.PERMANENT


def test_edit_file_preserves_crlf_line_endings(workspace: Path) -> None:
    target = workspace / "crlf.py"
    target.write_bytes(b"def run():\r\n    return 1\r\n")

    result = _tools(workspace).execute(
        _request("edit_file", path="crlf.py", old="return 1", new="return 2")
    )

    assert result.ok
    assert target.read_bytes() == b"def run():\r\n    return 2\r\n"


def test_write_file_into_git_metadata_is_denied(workspace: Path) -> None:
    result = _tools(workspace).execute(
        _request("write_file", path=".git/config", content="[diff]\n\texternal = /bin/sh")
    )

    assert not result.ok
    assert result.error_code == "SANDBOX_POLICY"
    assert "Git metadata directory" in result.observation


def test_paths_with_nul_or_control_characters_are_denied(workspace: Path) -> None:
    tools = _tools(workspace)
    for bad in ("a\x00b.py", "a\nb.py", "a\x1bb.py"):
        result = tools.execute(_request("write_file", path=bad, content="x"))
        assert not result.ok
        assert result.error_code == "SANDBOX_POLICY"
