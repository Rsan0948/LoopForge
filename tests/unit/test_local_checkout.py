"""Pinned, self-contained Git checkouts do not duplicate mutable files or host pointers."""

import hashlib
import shutil
from pathlib import Path

import pytest

from loopforge.adapters import file_copy
from loopforge.adapters.git_checkout import local_checkout
from loopforge.adapters.git_workspace import GitWorkspaceManager, git_process
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.ports.workspace import WorkspaceError


def test_checkout_preserves_history_dirty_source_and_independent_objects(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path / "sources").materialize(
        FixtureSpec(
            fixture_id="source", files=(FixtureFile(path="src/value.py", content="A = 1\n"),)
        )
    )
    original = workspace.root
    baseline = workspace.base_revision
    (original / "src/value.py").write_text("A = 2\n")
    git_process(original, "commit", "-qam", "second revision")
    head = git_process(original, "rev-parse", "HEAD").stdout.decode().strip()
    (original / "src/value.py").write_text("DIRTY\n")
    before = hashlib.sha256((original / ".git/index").read_bytes()).hexdigest()
    target = tmp_path / "candidate"
    local_checkout(original, baseline, target, "candidate/test")
    assert (target / "src/value.py").read_text() == "A = 1\n"
    assert (original / "src/value.py").read_text() == "DIRTY\n"
    assert hashlib.sha256((original / ".git/index").read_bytes()).hexdigest() == before
    assert git_process(target, "rev-parse", "HEAD").stdout.decode().strip() == baseline
    assert git_process(target, "show", head + ":src/value.py").stdout == b"A = 2\n"
    assert not git_process(target, "status", "--porcelain").stdout
    assert not git_process(target, "remote").stdout
    assert not (target / ".git/objects/info/alternates").exists()
    object_path = next(p for p in (original / ".git/objects").rglob("*") if p.is_file())
    copied = target / object_path.relative_to(original)
    assert copied.stat().st_ino != object_path.stat().st_ino
    copied.chmod(0o644)
    copied.write_bytes(b"corrupt only this candidate")
    assert object_path.read_bytes() != copied.read_bytes()
    # A separate healthy checkout survives removal of its original repository.
    healthy = tmp_path / "healthy"
    local_checkout(original, baseline, healthy, "candidate/healthy")
    shutil.rmtree(original)
    assert git_process(healthy, "show", head + ":src/value.py").stdout == b"A = 2\n"
    assert git_process(healthy, "fsck", "--full").returncode == 0


def test_matching_source_files_use_the_independent_copy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loopforge.adapters import git_checkout  # noqa: PLC0415

    workspace = GitWorkspaceManager(tmp_path / "sources").materialize(
        FixtureSpec(
            fixture_id="source", files=(FixtureFile(path="executable", content="echo yes\n"),)
        )
    )
    calls: list[Path] = []
    actual = file_copy.copy_file

    def recording(source: Path, target: Path, *, require_clone: bool = False) -> bool:
        calls.append(source)
        return actual(source, target, require_clone=require_clone)

    monkeypatch.setattr(git_checkout, "copy_file", recording)
    candidate = tmp_path / "candidate"
    local_checkout(workspace.root, workspace.base_revision, candidate, "candidate/test")
    assert workspace.root / "executable" in calls
    assert (candidate / "executable").stat().st_ino != (workspace.root / "executable").stat().st_ino
    (candidate / "executable").write_text("changed\n")
    assert (workspace.root / "executable").read_text() == "echo yes\n"


def test_checkout_refuses_external_object_dependencies_and_existing_targets(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path / "sources").materialize(
        FixtureSpec(fixture_id="source", files=(FixtureFile(path="file", content="value\n"),))
    )
    target = tmp_path / "existing"
    target.mkdir()
    (target / "retained").write_text("keep")
    with pytest.raises(WorkspaceError, match="already exists"):
        local_checkout(workspace.root, workspace.base_revision, target, "candidate/test")
    assert (target / "retained").read_text() == "keep"
    (workspace.root / ".git/objects/info/alternates").write_text("/outside/objects\n")
    with pytest.raises(WorkspaceError, match="independent"):
        local_checkout(workspace.root, workspace.base_revision, tmp_path / "new", "candidate/test")
    assert not (tmp_path / "new").exists()


def test_dirty_symlinks_are_not_followed_and_failed_object_copies_are_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno  # noqa: PLC0415

    from loopforge.adapters import git_checkout  # noqa: PLC0415

    workspace = GitWorkspaceManager(tmp_path / "sources").materialize(
        FixtureSpec(fixture_id="source", files=(FixtureFile(path="file", content="pinned\n"),))
    )
    outside = tmp_path / "outside"
    outside.write_text("private outside bytes")
    path = workspace.root / "file"
    path.unlink()
    path.symlink_to(outside)
    candidate = tmp_path / "candidate"
    local_checkout(workspace.root, workspace.base_revision, candidate, "candidate/test")
    assert (candidate / "file").read_text() == "pinned\n"
    assert not (candidate / "file").is_symlink()
    assert outside.read_text() == "private outside bytes"

    def full(_source: Path, _target: Path, *, require_clone: bool = False) -> bool:
        del require_clone
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(git_checkout, "copy_file", full)
    with pytest.raises(OSError, match="disk full"):
        local_checkout(
            workspace.root, workspace.base_revision, tmp_path / "failed", "candidate/fail"
        )
    assert not (tmp_path / "failed").exists()
    assert not list(tmp_path.glob(".failed.preparing-*"))


def test_source_linked_worktree_becomes_a_self_contained_checkout(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path / "sources").materialize(
        FixtureSpec(fixture_id="source", files=(FixtureFile(path="file", content="pinned\n"),))
    )
    linked = tmp_path / "linked"
    git_process(workspace.root, "worktree", "add", "--detach", str(linked), workspace.base_revision)
    candidate = tmp_path / "candidate"
    local_checkout(linked, workspace.base_revision, candidate, "candidate/test")
    assert (candidate / ".git").is_dir()
    assert not (candidate / ".git/commondir").exists()
    assert not (candidate / ".git/objects/info/alternates").exists()
    shutil.rmtree(linked)
    shutil.rmtree(workspace.root)
    assert git_process(candidate, "show", "HEAD:file").stdout == b"pinned\n"


def test_unreadable_dirty_worktree_does_not_prevent_checkout_of_pinned_blob(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path / "sources").materialize(
        FixtureSpec(fixture_id="source", files=(FixtureFile(path="file", content="pinned\n"),))
    )
    source = workspace.root / "file"
    source.chmod(0)
    candidate = tmp_path / "candidate"
    local_checkout(workspace.root, workspace.base_revision, candidate, "candidate/test")
    assert (candidate / "file").read_text() == "pinned\n"
    assert source.stat().st_mode & 0o777 == 0
