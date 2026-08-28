"""Unit tests for the Git workspace manager adapter (offline, hermetic).

These tests construct real Git repositories locally. No network access occurs:
fixture repositories are created with ``git init`` and never have remotes.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.domain.types import WorkspaceId
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.ports.workspace import WorkspaceError

_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git executable unavailable; workspace adapter tests require the Git CLI",
)

pytestmark = _REQUIRES_GIT


def _fixture() -> FixtureSpec:
    return FixtureSpec(
        fixture_id="sample",
        files=(
            FixtureFile(path="module.py", content="value = 1\n"),
            FixtureFile(path="tests/__init__.py", content=""),
            FixtureFile(path="tests/test_module.py", content="assert True\n"),
            FixtureFile(path=".gitignore", content="__pycache__/\n"),
        ),
        solution=(FixtureFile(path="module.py", content="value = 2\n"),),
    )


def _manager(tmp_path: Path) -> GitWorkspaceManager:
    return GitWorkspaceManager(tmp_path / "workspaces")


def test_materialize_creates_clean_workspace_with_deterministic_base_revision(
    tmp_path: Path,
) -> None:
    first = _manager(tmp_path / "a").materialize(_fixture())
    second = _manager(tmp_path / "b").materialize(_fixture())

    assert first.workspace_id == "sample"
    assert first.root.is_dir()
    assert (first.root / ".git").is_dir()
    assert first.status().clean
    # Fixed identity/dates plus hermetic config make the base commit reproducible.
    assert first.base_revision == second.base_revision
    assert first.diff() == ""


def test_materialize_supports_explicit_workspace_id(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture(), workspace_id=WorkspaceId("assigned-1"))
    assert workspace.workspace_id == "assigned-1"
    assert workspace.root.name == "assigned-1"


def test_adopt_existing_uses_current_head_without_overwriting(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    original = manager.materialize(_fixture(), workspace_id=WorkspaceId("source"))
    adopted = manager.adopt_existing(original.root, workspace_id=WorkspaceId("integration"))

    assert adopted.workspace_id == "integration"
    assert adopted.base_revision == original.base_revision
    assert adopted.status().clean
    assert (adopted.root / "module.py").read_text(encoding="utf-8") == "value = 1\n"


def test_adopt_existing_rejects_non_repository(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="local Git worktree"):
        _manager(tmp_path).adopt_existing(tmp_path / "missing")


def test_materialize_rejects_invalid_ids_and_existing_workspaces(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(WorkspaceError, match="plain name without path separators"):
        manager.materialize(_fixture(), workspace_id=WorkspaceId("a/b"))
    with pytest.raises(WorkspaceError, match="workspace_id cannot be empty"):
        manager.materialize(_fixture(), workspace_id=WorkspaceId(" "))
    manager.materialize(_fixture())
    with pytest.raises(WorkspaceError, match="workspace already exists"):
        manager.materialize(_fixture())


def test_manager_requires_git_executable(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="git executable failed to start"):
        GitWorkspaceManager(tmp_path / "ws", git_executable="/nonexistent/git").materialize(
            _fixture()
        )


def test_status_and_diff_track_modified_files(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    (workspace.root / "module.py").write_text("value = 2\n", encoding="utf-8")

    status = workspace.status()

    assert status.changed == ("module.py",)
    assert status.untracked == ()
    patch = workspace.diff()
    assert "diff --git a/module.py b/module.py" in patch
    assert "-value = 1" in patch
    assert "+value = 2" in patch


def test_status_reports_ignored_files_as_worktree_deviations(tmp_path: Path) -> None:
    # A writable .gitignore must never hide files from the verifier or the
    # evidence trail: ignored files are reported as untracked deviations, and
    # reset() reclaims them.
    workspace = _manager(tmp_path).materialize(_fixture())
    cache = workspace.root / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-312.pyc").write_bytes(b"\x00")

    status = workspace.status()
    assert not status.clean
    assert "__pycache__/" in status.untracked

    # The collapsed ignored directory is reported but never rendered as content.
    patch = workspace.diff()
    assert "__pycache__/" in patch
    assert "not rendered" in patch

    workspace.reset()
    assert workspace.status().clean
    assert not cache.exists()


def test_diff_marks_binary_untracked_files_without_falsifying_content(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    (workspace.root / "blob.bin").write_bytes(b"\x00\x01\x02\xff")

    patch = workspace.diff()

    assert "untracked binary file 'blob.bin' not rendered" in patch
    assert "\x00" not in patch


def test_diff_refuses_control_character_file_names(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    (workspace.root / "evil\nforged-line.py").write_text("x = 1\n", encoding="utf-8")

    patch = workspace.diff()

    assert "unsafe name" in patch
    # The raw name (with its real newline) must never land in the evidence
    # document; only the escaped ``repr`` form may appear.
    assert "evil\nforged-line.py" not in patch


def test_revert_restores_the_base_revision_not_the_index(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    target = workspace.root / "module.py"
    target.write_text("value = 2\n", encoding="utf-8")
    # Stage attacker-controlled content: revert must converge to the base
    # revision, never to whatever the index holds.
    subprocess.run(["git", "add", "module.py"], cwd=workspace.root, check=True, capture_output=True)
    target.write_text("value = 3\n", encoding="utf-8")

    workspace.checkout(("module.py",))

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert workspace.status().clean


def test_checkout_treats_paths_as_literals_never_pathspecs(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    (workspace.root / "module.py").write_text("value = 2\n", encoding="utf-8")
    (workspace.root / "notes.txt").write_text("hello\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="backslashes or control characters"):
        workspace.checkout(("module.py\n",))
    # A glob pathspec must not fan out across the worktree: pathspec magic is
    # rejected at the adapter boundary, before Git ever sees it.
    with pytest.raises(WorkspaceError, match="glob or pathspec magic"):
        workspace.checkout(("*.py",))
    with pytest.raises(WorkspaceError, match="glob or pathspec magic"):
        workspace.checkout((":(top)module.py",))
    # Neither file was reverted by the magic attempts.
    assert (workspace.root / "module.py").read_text(encoding="utf-8") == "value = 2\n"


def test_checkout_rejects_git_metadata_paths(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())

    with pytest.raises(WorkspaceError, match="Git metadata directory"):
        workspace.checkout((".git/config",))


def test_status_survives_non_utf8_file_names(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    # Git filenames are arbitrary bytes; status/diff must never decode-crash.
    # Some filesystems (APFS) reject non-UTF-8 names outright; the pin runs
    # where such names are creatable (for example Linux CI).
    try:
        (workspace.root / "\udcff\udcfe.py").write_text("x = 1\n", encoding="utf-8")
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 file names")

    status = workspace.status()

    assert any("py" in path for path in status.untracked)


def test_diff_renders_untracked_new_files_without_index_mutation(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    (workspace.root / "notes.txt").write_text("hello\n", encoding="utf-8")

    patch = workspace.diff()

    assert "--- /dev/null" in patch
    assert "+++ b/notes.txt" in patch
    assert "+hello" in patch
    # The render path must not stage anything: the file stays untracked.
    assert workspace.status().untracked == ("notes.txt",)


def test_diff_never_follows_untracked_symlinks(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    secret_dir = tmp_path_factory.mktemp("host-secret")
    secret = secret_dir / "secret.txt"
    secret.write_text("host-secret-content\n", encoding="utf-8")
    (workspace.root / "link.txt").symlink_to(secret)
    link_dir = workspace.root / "linked"
    link_dir.symlink_to(secret_dir, target_is_directory=True)

    patch = workspace.diff()

    assert "host-secret-content" not in patch
    assert "not rendered" in patch


def test_checkout_reverts_modified_file(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    target = workspace.root / "module.py"
    target.write_text("value = 99\n", encoding="utf-8")

    workspace.checkout(("module.py",))

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert workspace.status().clean


def test_checkout_validates_paths(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    with pytest.raises(WorkspaceError, match="checkout requires at least one path"):
        workspace.checkout(())
    with pytest.raises(WorkspaceError, match="checkout path cannot be empty"):
        workspace.checkout((" ",))
    with pytest.raises(WorkspaceError, match="must be relative"):
        workspace.checkout(("../escape.py",))
    with pytest.raises(WorkspaceError, match="must be relative"):
        workspace.checkout(("/etc/passwd",))


def test_checkout_unknown_path_raises_workspace_error(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    with pytest.raises(WorkspaceError, match="git checkout"):
        workspace.checkout(("untracked-new-file.py",))


def test_reset_restores_base_revision_and_removes_untracked(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    (workspace.root / "module.py").write_text("value = 99\n", encoding="utf-8")
    stray = workspace.root / "stray.txt"
    stray.write_text("stray\n", encoding="utf-8")

    workspace.reset()

    assert (workspace.root / "module.py").read_text(encoding="utf-8") == "value = 1\n"
    assert not stray.exists()
    assert workspace.status().clean


def test_git_failure_surfaces_as_workspace_error(tmp_path: Path) -> None:
    workspace = _manager(tmp_path).materialize(_fixture())
    broken = replace(workspace, _git="/nonexistent/git")
    with pytest.raises(WorkspaceError, match="git executable failed to start"):
        broken.status()


def test_tampered_repo_metadata_fails_closed_before_host_git(tmp_path: Path) -> None:
    # Untrusted workload content (via tools or sandboxed commands on the bind
    # mount) must never steer host-side Git execution: any change to
    # .git/config or .git/info/attributes after materialization fails closed.
    workspace = _manager(tmp_path).materialize(_fixture())
    config = workspace.root / ".git" / "config"
    original = config.read_bytes()
    config.write_bytes(original + b"\n[diff \"x\"]\n\ttextconv = /bin/sh -c 'touch /tmp/pwned'\n")

    with pytest.raises(WorkspaceError, match="repository metadata changed"):
        workspace.status()
    with pytest.raises(WorkspaceError, match="repository metadata changed"):
        workspace.diff()

    config.write_bytes(original)
    assert workspace.status().clean

    # Creating .git/info/attributes (absent at materialization) is tamper too.
    attributes = workspace.root / ".git" / "info" / "attributes"
    attributes.write_text("module.py diff=x\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="repository metadata changed"):
        workspace.status()


def test_failed_materialize_removes_the_half_built_workspace(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    fixture = _fixture()
    with pytest.raises(WorkspaceError, match="git executable failed to start"):
        GitWorkspaceManager(tmp_path / "workspaces", git_executable="/nonexistent/git").materialize(
            fixture
        )

    assert not (manager.workspaces_root / fixture.fixture_id).exists()
    # A retry with a working Git succeeds cleanly.
    assert manager.materialize(fixture).status().clean


def test_worker_worktrees_are_isolated_and_merge_in_order(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    integration = manager.materialize(_fixture(), workspace_id=WorkspaceId("integration"))
    worker = manager.add_worker_worktree(integration, worker_id="one")

    assert (worker.root / ".git").is_file()
    (worker.root / "module.py").write_text("value = 2\n", encoding="utf-8")
    assert (integration.root / "module.py").read_text(encoding="utf-8") == "value = 1\n"

    manager.commit_worker(worker, message="worker one repair")
    revision = manager.merge_worker(integration, worker_id="one")

    assert revision is not None
    assert (integration.root / "module.py").read_text(encoding="utf-8") == "value = 2\n"


def test_worker_merge_conflict_is_aborted(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    integration = manager.materialize(_fixture(), workspace_id=WorkspaceId("integration"))
    first = manager.add_worker_worktree(integration, worker_id="one")
    second = manager.add_worker_worktree(integration, worker_id="two")
    (first.root / "module.py").write_text("value = 2\n", encoding="utf-8")
    (second.root / "module.py").write_text("value = 3\n", encoding="utf-8")
    manager.commit_worker(first, message="first")
    manager.commit_worker(second, message="second")

    assert manager.merge_worker(integration, worker_id="one") is not None
    assert manager.merge_worker(integration, worker_id="two") is None
    assert (integration.root / "module.py").read_text(encoding="utf-8") == "value = 2\n"
    assert integration.status().clean
