"""Capture an explicit dirty checkout into an independent local checkpoint.

The original index, branch and files are read only. This is a Git snapshot,
not an execution sandbox. Ignored files are excluded and listed in the manifest.
"""

import hashlib
import json
import stat
from pathlib import Path

from loopforge.entrypoints.campaign_coding import local_git_bytes, prepare_checkout


def capture(root: Path) -> tuple[str, dict[str, tuple[bytes, int]], str, list[str]]:
    root = root.resolve()
    head = local_git_bytes(root, "rev-parse", "HEAD").decode().strip()
    names = (
        local_git_bytes(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        .decode()
        .split("\0")
    )
    files: dict[str, tuple[bytes, int]] = {}
    total = 0
    for name in sorted(set(names) - {""}):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            msg = "Checkpoint cannot include symlinks, submodules or special files"
            raise ValueError(msg)
        if path.stat().st_size > 8_000_000 or len(files) >= 20000:
            msg = "Checkpoint exceeds file limits"
            raise ValueError(msg)
        body = path.read_bytes()
        total += len(body)
        if total > 64_000_000:
            msg = "Checkpoint exceeds byte limit"
            raise ValueError(msg)
        files[name] = (body, stat.S_IMODE(path.stat().st_mode))
    index = hashlib.sha256(
        local_git_bytes(root, "diff", "--cached", "--binary", head, "--")
    ).hexdigest()
    ignored = (
        local_git_bytes(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard")
        .decode()
        .split("\0")
    )
    return head, files, index, sorted(set(ignored) - {""})


def snapshot_checkpoint(source: Path, directory: Path, revision: str) -> tuple[Path, str]:
    manifest_path = directory / "checkpoint.json"
    target = directory / "checkpoint"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["source"] != str(source.resolve()) or manifest["original_revision"] != revision:
            msg = "Checkpoint intake is immutable"
            raise ValueError(msg)
        actual = local_git_bytes(target, "rev-parse", "HEAD").decode().strip()
        if actual != manifest["snapshot_revision"] or local_git_bytes(
            target, "status", "--porcelain"
        ):
            msg = "Recorded checkpoint changed"
            raise ValueError(msg)
        return target, actual
    before = capture(source)
    if before[0] != revision:
        msg = "Dirty checkpoint requires the explicitly selected HEAD revision"
        raise ValueError(msg)
    if target.exists():
        # An incomplete isolated snapshot is never silently reused.
        msg = "Incomplete checkpoint retained; use a fresh goal directory"
        raise ValueError(msg)
    prepare_checkout(source, revision, target, "checkpoint/source")
    tracked = local_git_bytes(target, "ls-files", "-z").decode().split("\0")
    for name in set(tracked) - set(before[1]) - {""}:
        path = target / name
        if path.is_file() or path.is_symlink():
            path.unlink()
    for name, (body, mode) in before[1].items():
        path = target / name
        if path.is_symlink() or any(
            parent.is_symlink() for parent in path.parents if parent != target.parent
        ):
            msg = "Unsafe checkpoint path"
            raise ValueError(msg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(mode)
    local_git_bytes(target, "add", "--force", "--all", "--", ".")
    local_git_bytes(
        target,
        "-c",
        "user.name=LoopForge Checkpoint",
        "-c",
        "user.email=checkpoint@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "LoopForge operator checkout snapshot",
    )
    if capture(source) != before:
        msg = "Source changed while capturing checkpoint; isolated work retained"
        raise ValueError(msg)
    snapshot = local_git_bytes(target, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "source": str(source.resolve()),
        "original_revision": revision,
        "snapshot_revision": snapshot,
        "staged_diff_sha256": before[2],
        "excluded_ignored_paths": before[3],
        "files": {
            name: {"sha256": hashlib.sha256(body).hexdigest(), "mode": mode}
            for name, (body, mode) in before[1].items()
        },
    }
    pending = manifest_path.with_suffix(".pending")
    pending.write_text(json.dumps(manifest, sort_keys=True, indent=2))
    pending.replace(manifest_path)
    return target, snapshot
