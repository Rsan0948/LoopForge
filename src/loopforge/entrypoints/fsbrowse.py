"""Read-only filesystem browse + test-harness detection for the operator console.

Trusted-operator local tool (D10): the server binds loopback with no
authentication, and these helpers keep the new read surface minimal —
subdirectory names and repository marker files only, NEVER the contents of
arbitrary files, and no write sibling exists. Browsing is deliberately
unjailed (VS Code open-folder style): the single local operator picks any
directory on their own machine.

Everything returned here is operator-facing DRAFT material: session creation
still validates through :func:`loopforge.entrypoints.profile.load_profile`'s
fail-closed contract, so a suggestion can never widen authority (rules
14/16).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Final, cast

_MAX_ENTRIES: Final = 500
_PYTEST_MARKERS: Final = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")
# No trailing slashes: the domain patch-constraint contract rejects empty
# path segments (domain/workspace.py), so "src/" would fail creation.
_PREFIX_CANDIDATES: Final = ("src", "tests", "test", "app", "lib")
_SHORTCUT_CANDIDATES: Final = ("Developer", "Projects", "work")
_MAKE_TEST_TARGET: Final = re.compile(r"^test\s*:", re.MULTILINE)


class UnknownFsPathError(Exception):
    """Raised when a browse/detect path does not name an existing directory."""


def _require_directory(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        msg = f"path must be absolute, got {raw!r}"
        raise ValueError(msg)
    if not path.is_dir():
        msg_2 = f"no such directory: {path}"
        raise UnknownFsPathError(msg_2)
    return path


def _is_git_worktree(path: Path) -> bool:
    try:
        return (path / ".git").exists()
    except OSError:
        return False


def browse_directories(raw_path: str | None) -> dict[str, object]:
    """List the subdirectories of ``raw_path`` (default: the user's home).

    Read-only and directory-names-only; unreadable entries are skipped, an
    unreadable directory is honestly noted, and an over-large listing is
    flagged with ``truncated`` rather than silently clipped.
    """
    if raw_path is None or raw_path.strip() == "":
        path = Path.home()
    else:
        path = _require_directory(raw_path)
    entries: list[dict[str, object]] = []
    notes: list[str] = []
    truncated = False
    try:
        children = sorted(path.iterdir(), key=lambda child: child.name.lower())
    except PermissionError:
        children = []
        notes.append(f"directory is not readable: {path}")
    for child in children:
        if len(entries) >= _MAX_ENTRIES:
            truncated = True
            break
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue  # vanished or unreadable between listing and stat
        entries.append(
            {
                "name": child.name,
                "is_git_worktree": _is_git_worktree(child),
                "is_hidden": child.name.startswith("."),
            }
        )
    shortcuts: list[str] = []
    for candidate in (
        Path.home(),
        *(Path.home() / name for name in _SHORTCUT_CANDIDATES),
        Path("/"),
    ):
        text = str(candidate)
        if candidate.is_dir() and text not in shortcuts:
            shortcuts.append(text)
    return {
        "path": str(path),
        "parent": None if path.parent == path else str(path.parent),
        "is_git_worktree": _is_git_worktree(path),
        "entries": entries,
        "truncated": truncated,
        "shortcuts": shortcuts,
        "notes": notes,
    }


def _npm_has_test_script(package_json: Path) -> bool:
    try:
        # Marker-file parsing stays Any-quarantined here (rule 8): the JSON
        # shape is untrusted repository content, validated by isinstance.
        raw: Any = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    scripts = cast("dict[str, Any]", raw).get("scripts")
    if not isinstance(scripts, dict):
        return False
    return isinstance(cast("dict[str, Any]", scripts).get("test"), str)


def _makefile_has_test_target(makefile: Path) -> bool:
    try:
        content = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _MAKE_TEST_TARGET.search(content) is not None


def detect_harness(raw_path: str) -> dict[str, object]:
    """Suggest inline-profile checks from a repository's marker files.

    First match wins (Python markers, then npm, then make); every heuristic
    is honestly narrated in ``notes``. Absence is reported, never filled
    in: an unrecognized repository yields empty ``checks`` plus an explicit
    "no test harness detected" note.
    """
    path = _require_directory(raw_path)
    checks: list[dict[str, object]] = []
    notes: list[str] = []
    if any((path / marker).is_file() for marker in _PYTEST_MARKERS):
        checks.append(
            {
                "name": "tests",
                "kind": "TEST",
                "argv": ["{python}", "-m", "pytest", "-q"],
                "timeout_seconds": 120,
            }
        )
        if not (path / ".venv" / "bin" / "python").is_file():
            notes.append(
                "no .venv/bin/python found — the {python} token needs "
                "sandbox.local_python or a container image in local mode"
            )
    elif _npm_has_test_script(path / "package.json"):
        npm = shutil.which("npm")
        if npm is not None:
            checks.append(
                {
                    "name": "tests",
                    "kind": "TEST",
                    "argv": [npm, "test"],
                    "timeout_seconds": 120,
                }
            )
        else:
            notes.append(
                "package.json declares a test script but npm is not on PATH — "
                "add the check manually with an absolute executable"
            )
    elif _makefile_has_test_target(path / "Makefile"):
        make = shutil.which("make")
        if make is not None:
            checks.append(
                {
                    "name": "tests",
                    "kind": "TEST",
                    "argv": [make, "test"],
                    "timeout_seconds": 120,
                }
            )
        else:
            notes.append(
                "Makefile declares a test target but make is not on PATH — "
                "add the check manually with an absolute executable"
            )
    if not checks and not notes:
        notes.append("no test harness detected")
    prefixes = [prefix for prefix in _PREFIX_CANDIDATES if (path / prefix).is_dir()]
    if not prefixes:
        notes.append(
            "no conventional source directories found — set acceptance.allowed_prefixes manually"
        )
    if not _is_git_worktree(path):
        notes.append("not a git worktree (no .git) — session creation requires one")
    return {
        "is_git_worktree": _is_git_worktree(path),
        "checks": checks,
        "required": [check["name"] for check in checks],
        "allowed_prefixes": prefixes,
        "notes": notes,
    }
