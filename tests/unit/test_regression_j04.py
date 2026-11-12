from contextlib import suppress
from pathlib import Path

import pytest

from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.domain.workspace import FixtureFile, FixtureSpec


def test_revert_never_deletes_a_file_outside_the_workspace(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path / "workspaces").materialize(
        FixtureSpec(
            fixture_id="revert-audit", files=(FixtureFile(path="src/a.py", content="VALUE = 1\n"),)
        )
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("This temporary file is outside the workspace")
    (workspace.root / "link").symlink_to(outside, target_is_directory=True)
    with suppress(Exception):  # refusing the operation is safe if the file survives
        workspace.checkout(("link/victim.txt",))
    assert victim.exists(), "Revert deleted the temporary file outside the workspace"


def test_ancestor_replacement_between_validation_and_unlink_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loopforge.adapters.git_workspace import GitWorkspace  # noqa: PLC0415

    workspace = GitWorkspaceManager(tmp_path / "workspaces").materialize(
        FixtureSpec(fixture_id="race", files=(FixtureFile(path="src/a.py", content="VALUE = 1\n"),))
    )
    inside = workspace.root / "safe"
    inside.mkdir()
    (inside / "victim.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("outside")

    def replace_ancestor(current: GitWorkspace, _path: str) -> bool:
        inside.rename(current.root / "retained")
        inside.symlink_to(outside, target_is_directory=True)
        return False

    monkeypatch.setattr(GitWorkspace, "_exists_at_base", replace_ancestor)
    with suppress(Exception):
        workspace.checkout(("safe/victim.txt",))
    assert victim.read_text() == "outside"
    assert (workspace.root / "retained/victim.txt").read_text() == "inside"
