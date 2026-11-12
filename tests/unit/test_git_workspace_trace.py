"""Git's own event trace reaches workspace Git only through the operator's environment."""

import json
from pathlib import Path

import pytest

from loopforge.adapters import git_workspace
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.domain.workspace import FixtureFile, FixtureSpec


def test_an_operator_trace_records_each_git_process_and_its_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = tmp_path / "git-trace2.jsonl"
    monkeypatch.setenv("LOOPFORGE_GIT_TRACE2", str(trace))
    workspace = GitWorkspaceManager(tmp_path / "ws").materialize(
        FixtureSpec(fixture_id="f", files=(FixtureFile(path="a.txt", content="x\n"),))
    )
    with pytest.raises(git_workspace.WorkspaceError):
        workspace.run_git("rev-parse", "no-such-revision")
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert any(e.get("event") == "start" and "rev-parse" in e.get("argv", []) for e in events)
    assert any(e.get("event") == "error" for e in events)


def test_without_the_operator_setting_or_with_a_relative_path_nothing_is_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOOPFORGE_GIT_TRACE2", raising=False)
    assert "GIT_TRACE2_EVENT" not in git_workspace._git_environment()  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setenv("LOOPFORGE_GIT_TRACE2", "relative/trace.jsonl")
    assert "GIT_TRACE2_EVENT" not in git_workspace._git_environment()  # pyright: ignore[reportPrivateUsage]
