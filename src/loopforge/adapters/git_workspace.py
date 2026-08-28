"""Git-backed workspace manager adapter (offline, hermetic).

Implements the ``WorkspaceManagerPort``/``WorkspacePort`` primitives — status,
diff, checkout, reset — through the local Git CLI. Git stays in the adapter
layer (AGENTS.md rule 1): the domain sees only plain vocabulary.

Hermetic guarantees:

- no network: repositories are created locally with ``git init`` and no
  remotes ever exist; ``GIT_TERMINAL_PROMPT=0`` makes any accidental prompt
  impossible;
- no host config leakage: ``GIT_CONFIG_NOSYSTEM=1`` and
  ``GIT_CONFIG_GLOBAL=<devnull>`` isolate the workload from operator Git
  configuration, and ``GIT_ATTR_NOSYSTEM=1`` ignores system gitattributes;
- literal paths: checkout path validation rejects glob/pathspec-magic
  characters outright (Git does not honor ``GIT_LITERAL_PATHSPEMS`` for
  every subcommand, so the adapter enforces literal semantics itself);
- deterministic fixture base revisions: author/committer identity and dates
  are fixed constants, so the same fixture always yields the same base commit;
- deterministic decoding: child output is decoded as UTF-8 with
  ``surrogateescape``, never the host locale's preferred encoding, so hostile
  or non-UTF-8 filenames cannot crash status/diff and behave identically on
  every host;
- confinement: every Git invocation runs with ``cwd`` inside the assigned
  workspace root, with a hard wall-clock timeout; untracked-file diff
  rendering never follows symlinks, caps bytes per file, and refuses binary or
  control-character file names, so hostile fixture content can neither leak
  host files into evidence artifacts nor falsify or exhaust them;
- metadata integrity: repository metadata (``.git/config`` and
  ``.git/info/attributes``) is fingerprinted at materialization and verified
  before every host-side Git invocation. Workload content — whether written
  through tools or by sandboxed commands on the bind mount — can therefore
  never steer host Git execution (textconv/filter drivers, fsmonitor hooks,
  config includes); any tamper fails closed with ``WorkspaceError``;
- honest change inventory: ``status()`` reports ignored files too (a writable
  ``.gitignore`` must not hide worktree deviations from the verifier), and
  ``reset()`` reclaims them (``clean -fdx``).

New-file hunks in ``diff()`` are rendered with ``difflib`` (evidence rendering
for files Git does not track yet); tracked-file patches come from
``git diff <base>`` verbatim. The combined document is the exact patch evidence
recorded by the repair workload's artifact collector.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from loopforge.domain.types import WorkspaceId
from loopforge.domain.workspace import FixtureSpec, WorkspaceStatus
from loopforge.ports.workspace import WorkspaceError

_FIXED_IDENTITY: Final = "LoopForge Fixture <fixture@loopforge.invalid>"
_FIXED_DATE: Final = "2001-01-01T00:00:00+00:00"
_STDERR_BUDGET: Final = 2_000
_GIT_DIR_NAME: Final = ".git"
_GIT_TIMEOUT_SECONDS: Final = 30.0
_MAX_RENDER_BYTES: Final = 1_000_000
_MAX_WORKSPACE_ID_LENGTH: Final = 128


def _git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_LITERAL_PATHSPEMS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": _FIXED_IDENTITY,
        "GIT_AUTHOR_EMAIL": "fixture@loopforge.invalid",
        "GIT_AUTHOR_DATE": _FIXED_DATE,
        "GIT_COMMITTER_NAME": _FIXED_IDENTITY,
        "GIT_COMMITTER_EMAIL": "fixture@loopforge.invalid",
        "GIT_COMMITTER_DATE": _FIXED_DATE,
    }


def _has_unsafe_characters(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _validate_workspace_id(workspace_id: str) -> None:
    if not workspace_id.strip():
        msg = "workspace_id cannot be empty"
        raise WorkspaceError(msg)
    if any(char in workspace_id for char in "/\\") or workspace_id in {".", ".."}:
        msg_2 = "workspace_id must be a plain name without path separators"
        raise WorkspaceError(msg_2)
    if len(workspace_id) > _MAX_WORKSPACE_ID_LENGTH or _has_unsafe_characters(workspace_id):
        msg_3 = "workspace_id must not contain control characters or exceed 128 characters"
        raise WorkspaceError(msg_3)


def _validate_checkout_path(path: str) -> None:
    if not path.strip():
        msg = "checkout path cannot be empty"
        raise WorkspaceError(msg)
    if "\\" in path or _has_unsafe_characters(path):
        msg_3 = "checkout path must not contain backslashes or control characters"
        raise WorkspaceError(msg_3)
    if any(char in path for char in "*?[:"):
        # Glob/pathspec magic must never fan a single revert out across the
        # worktree; checkout paths are literal file paths only.
        msg_5 = "checkout path must be a literal path without glob or pathspec magic"
        raise WorkspaceError(msg_5)
    parts = path.split("/")
    if path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        msg_2 = "checkout path must be relative without '.', '..', or empty segments"
        raise WorkspaceError(msg_2)
    if parts[0] == _GIT_DIR_NAME:
        # Repository metadata is never a checkout target: a writable .git would
        # let workload content steer host-side Git execution.
        msg_4 = "checkout path cannot target the Git metadata directory"
        raise WorkspaceError(msg_4)


def _resolve_git_executable(git_executable: str | None) -> str:
    if git_executable is not None:
        return git_executable
    resolved = shutil.which("git")
    if resolved is None:
        msg = "git executable not found on PATH"
        raise WorkspaceError(msg)
    return resolved


@dataclass(frozen=True, slots=True, kw_only=True)
class GitWorkspace:
    """An assigned, revision-tracked workspace backed by a local Git repository."""

    _root: Path
    _git: str
    _workspace_id: WorkspaceId
    _base_revision: str
    _metadata_fingerprint: str

    @property
    def workspace_id(self) -> WorkspaceId:
        return self._workspace_id

    @property
    def root(self) -> Path:
        return self._root

    @property
    def base_revision(self) -> str:
        return self._base_revision

    def status(self) -> WorkspaceStatus:
        output = self.run_git(
            "status",
            "--porcelain=v1",
            "-z",
            "--no-renames",
            "--untracked-files=all",
            # Ignored files are worktree deviations too: a writable .gitignore
            # must never hide changes from the verifier or the evidence trail.
            "--ignored=matching",
        )
        changed: list[str] = []
        untracked: list[str] = []
        for entry in output.split("\0"):
            if not entry:
                continue
            marker, path = entry[:2], entry[3:]
            if marker in {"??", "!!"}:
                untracked.append(path)
            else:
                changed.append(path)
        return WorkspaceStatus(changed=tuple(sorted(changed)), untracked=tuple(sorted(untracked)))

    def diff(self) -> str:
        tracked = self.run_git("diff", "--no-color", "--no-ext-diff", self._base_revision, "--")
        parts = [tracked] if tracked.strip() else []
        for relative in self.status().untracked:
            rendered = self._render_new_file_diff(relative)
            if rendered:
                parts.append(rendered)
        return "".join(parts)

    def checkout(self, paths: tuple[str, ...]) -> None:
        if not paths:
            msg = "checkout requires at least one path"
            raise WorkspaceError(msg)
        for path in paths:
            _validate_checkout_path(path)
        # Restore from the base revision (index and worktree), never from the
        # possibly-staged index: revert converges to the recorded base.
        self.run_git("checkout", self._base_revision, "--", *paths)

    def reset(self) -> None:
        self.run_git("reset", "--hard", "-q", self._base_revision)
        # -x: reclaim ignored files too, so planted hidden files cannot
        # survive a reset and influence later verification runs.
        self.run_git("clean", "-fdqx")

    def _render_new_file_diff(self, relative: str) -> str:  # noqa: PLR0911 - each early return is a deliberate hostile-content guard; folding them would obscure the defenses
        if _has_unsafe_characters(relative):
            # Control characters in file names could forge lines inside the
            # evidence document; refuse to render them verbatim.
            return f"loopforge: untracked file with unsafe name {relative!r} not rendered\n"
        candidate = self._root
        for part in relative.split("/"):
            if not part:
                # Ignored directories appear with a trailing slash; there is no
                # file content to render for a collapsed directory entry.
                return f"loopforge: untracked non-regular file {relative!r} not rendered\n"
            candidate = candidate / part
            if candidate.is_symlink():
                # Never follow links: hostile content must not leak host files
                # into evidence artifacts.
                return f"loopforge: untracked non-regular file {relative!r} not rendered\n"
        if not candidate.is_relative_to(self._root):
            msg = f"untracked path escapes workspace: {relative!r}"
            raise WorkspaceError(msg)
        try:
            if not candidate.is_file():
                return f"loopforge: untracked non-regular file {relative!r} not rendered\n"
            if candidate.stat().st_size > _MAX_RENDER_BYTES:
                return f"loopforge: untracked file {relative!r} exceeds the render budget\n"
            raw = candidate.read_bytes()
        except FileNotFoundError:
            # Vanished between status() and rendering: report, never crash.
            return f"loopforge: untracked file {relative!r} vanished before rendering\n"
        except OSError as exc:
            msg_2 = f"untracked file {relative!r} cannot be read: {exc}"
            raise WorkspaceError(msg_2) from exc
        if b"\x00" in raw:
            return f"loopforge: untracked binary file {relative!r} not rendered\n"
        content = raw.decode("utf-8", errors="replace")
        rendered = difflib.unified_diff(
            [],
            content.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{relative}",
        )
        return "".join(rendered)

    def run_git(self, *args: str) -> str:
        if _metadata_fingerprint(self._root) != self._metadata_fingerprint:
            msg = (
                "repository metadata changed since materialization; "
                "host-side Git refuses to trust it"
            )
            raise WorkspaceError(msg)
        return _run_git(self._git, self._root, *args)


def _metadata_fingerprint(root: Path) -> str:
    """Fingerprint the repo metadata that could steer host-side Git execution.

    Covers ``.git/config`` (diff/filter drivers, fsmonitor hooks, includes)
    and ``.git/info/attributes`` (driver bindings), including their absence,
    so creation, modification, deletion, or symlink swaps all fail closed.
    """
    digest = hashlib.sha256()
    git_marker = root / _GIT_DIR_NAME
    if git_marker.is_file() and not git_marker.is_symlink():
        # Linked worktrees carry a `.git` pointer file. Pin the exact pointer
        # so workspace content cannot redirect later host-side Git calls.
        digest.update(b"worktree-pointer\x00")
        digest.update(git_marker.read_bytes())
    for relative in ("config", "info/attributes"):
        candidate = root / _GIT_DIR_NAME / relative
        digest.update(relative.encode("utf-8"))
        if candidate.is_file() and not candidate.is_symlink():
            digest.update(b"\x01")
            digest.update(candidate.read_bytes())
        else:
            digest.update(b"\x00")
    return digest.hexdigest()


def _run_git(git: str, root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            [git, *args],
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            # Pin the codec: the host locale must never decide whether a
            # hostile filename decodes or crashes the run.
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError as exc:
        msg = f"git executable failed to start: {exc}"
        raise WorkspaceError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg_3 = f"git {' '.join(args)} exceeded {_GIT_TIMEOUT_SECONDS}s"
        raise WorkspaceError(msg_3) from exc
    if completed.returncode != 0:
        stderr = completed.stderr[:_STDERR_BUDGET]
        msg_2 = f"git {' '.join(args)} failed (exit {completed.returncode}): {stderr}"
        raise WorkspaceError(msg_2)
    return completed.stdout


class GitWorkspaceManager:
    """Materializes code-owned fixture repositories into assigned workspaces.

    Each fixture becomes a fresh Git repository with a single deterministic
    base commit. Materializing into an already-existing workspace path fails
    loudly rather than silently reusing or overwriting state; a failed
    materialization removes the half-built workspace so a retry is possible.
    """

    def __init__(self, workspaces_root: str | Path, *, git_executable: str | None = None) -> None:
        root = Path(workspaces_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self._root = root
        self._git = _resolve_git_executable(git_executable)

    @property
    def workspaces_root(self) -> Path:
        return self._root

    def materialize(
        self, fixture: FixtureSpec, *, workspace_id: WorkspaceId | None = None
    ) -> GitWorkspace:
        assigned = workspace_id or WorkspaceId(fixture.fixture_id)
        _validate_workspace_id(str(assigned))
        target = self._root / str(assigned)
        if target.exists():
            msg = f"workspace already exists: {assigned}"
            raise WorkspaceError(msg)
        target.mkdir()
        try:
            _run_git(self._git, target, "init", "-q", "-b", "main")
            for item in fixture.files:
                path = target.joinpath(*item.path.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(item.content, encoding="utf-8", newline="")
            _run_git(self._git, target, "add", "-A")
            _run_git(self._git, target, "commit", "-q", "-m", f"fixture: {fixture.fixture_id}")
            base_revision = _run_git(self._git, target, "rev-parse", "HEAD").strip()
            metadata_fingerprint = _metadata_fingerprint(target)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return GitWorkspace(
            _root=target,
            _git=self._git,
            _workspace_id=WorkspaceId(str(assigned)),
            _base_revision=base_revision,
            _metadata_fingerprint=metadata_fingerprint,
        )

    def adopt_existing(
        self, repository: str | Path, *, workspace_id: WorkspaceId | None = None
    ) -> GitWorkspace:
        """Adopt a clean existing local Git checkout as an integration workspace.

        No files are copied or overwritten. The checkout must already be a Git
        worktree; LoopForge records its current HEAD and metadata fingerprint,
        then uses the same guarded Git surface as fixture repositories.
        """
        root = Path(repository).resolve()
        if not root.is_dir() or not (root / _GIT_DIR_NAME).exists():
            msg = "existing repository must be a local Git worktree"
            raise WorkspaceError(msg)
        assigned = workspace_id or WorkspaceId(root.name)
        _validate_workspace_id(str(assigned))
        try:
            base_revision = _run_git(self._git, root, "rev-parse", "HEAD").strip()
        except WorkspaceError:
            raise
        return GitWorkspace(
            _root=root,
            _git=self._git,
            _workspace_id=assigned,
            _base_revision=base_revision,
            _metadata_fingerprint=_metadata_fingerprint(root),
        )

    def add_worker_worktree(self, integration: GitWorkspace, *, worker_id: str) -> GitWorkspace:
        """Create an isolated linked worktree and worker branch."""
        _validate_workspace_id(worker_id)
        target = self._root / f"worker-{worker_id}"
        if target.exists():
            msg = f"worker workspace already exists: {worker_id}"
            raise WorkspaceError(msg)
        branch = f"worker/{worker_id}"
        integration.run_git(
            "worktree", "add", "-q", "-b", branch, str(target), integration.base_revision
        )
        return GitWorkspace(
            _root=target,
            _git=self._git,
            _workspace_id=WorkspaceId(worker_id),
            _base_revision=integration.base_revision,
            _metadata_fingerprint=_metadata_fingerprint(target),
        )

    @staticmethod
    def commit_worker(workspace: GitWorkspace, *, message: str) -> str:
        """Commit a verified worker patch and return its revision."""
        if not message.strip():
            msg = "worker commit message cannot be empty"
            raise WorkspaceError(msg)
        workspace.run_git("add", "-A")
        workspace.run_git("commit", "-q", "-m", message)
        return workspace.run_git("rev-parse", "HEAD").strip()

    @staticmethod
    def merge_worker(integration: GitWorkspace, *, worker_id: str) -> str | None:
        """Merge one worker in spawn order; return None after an aborted conflict."""
        _validate_workspace_id(worker_id)
        try:
            integration.run_git("merge", "--no-ff", "--no-edit", f"worker/{worker_id}")
        except WorkspaceError:
            with suppress(WorkspaceError):
                integration.run_git("merge", "--abort")
            return None
        return integration.run_git("rev-parse", "HEAD").strip()
