"""Self-contained local Git checkouts with copy-on-write storage when supported.

Every checkout has its own index, objects, refs and files. No hardlinks, remotes,
alternates or linked-worktree pointers are introduced. All original commit objects
remain available, including baselines needed by continuation patches.
"""

import hashlib
import os
import shutil
import stat
import uuid
from pathlib import Path

from loopforge.adapters.file_copy import copy_file
from loopforge.adapters.git_workspace import git_process
from loopforge.ports.workspace import WorkspaceError


def _copy_objects(source: Path, target: Path) -> None:
    if not stat.S_ISDIR(source.lstat().st_mode):
        msg = "Git object storage must be a real directory, not a symlink"
        raise WorkspaceError(msg)
    target.mkdir(exist_ok=True)
    for entry in source.iterdir():
        mode = entry.lstat().st_mode
        destination = target / entry.name
        if stat.S_ISDIR(mode):
            _copy_objects(entry, destination)
        elif stat.S_ISREG(mode):
            copy_file(entry, destination)
        else:
            msg = "Git object storage contains symlinks or special files"
            raise WorkspaceError(msg)


def _blob_matches(path: Path, identity: str, size: int, algorithm: str) -> bool:
    digest = hashlib.new(algorithm)
    digest.update(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == identity


def _converted_paths(target: Path, names: list[str]) -> set[str]:
    """Let Git materialize files whose attributes require checkout conversion."""
    converted: set[str] = set()
    for start in range(0, len(names), 32):
        raw = git_process(
            target,
            "check-attr",
            "--cached",
            "-z",
            "eol",
            "working-tree-encoding",
            "filter",
            "ident",
            "--",
            *names[start : start + 32],
        ).stdout.split(b"\0")
        for index in range(0, len(raw) - 1, 3):
            name, attribute, value = raw[index : index + 3]
            if (attribute == b"eol" and value.lower() == b"crlf") or (
                attribute != b"eol" and value not in {b"unspecified", b"unset"}
            ):
                converted.add(os.fsdecode(name))
    return converted


def _creation_modes(target: Path) -> dict[str, int]:
    """Observe the process creation mask without ever changing global umask."""
    modes: dict[str, int] = {}
    for git_mode, requested in (("100644", 0o666), ("100755", 0o777)):
        probe = target / ".git" / f".mode-probe-{uuid.uuid4().hex}"
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, requested)
        try:
            modes[git_mode] = stat.S_IMODE(os.fstat(descriptor).st_mode)
        finally:
            os.close(descriptor)
            probe.unlink()
    return modes


def _populate(source: Path, target: Path, revision: str, algorithm: str) -> None:
    git_process(target, "read-tree", revision)
    entries = git_process(target, "ls-tree", "-r", "-l", "-z", revision).stdout
    converted = _converted_paths(
        target, [os.fsdecode(entry.split(b"\t", 1)[1]) for entry in entries.split(b"\0") if entry]
    )
    creation_modes = _creation_modes(target)
    missing: list[str] = []
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        header, name_bytes = entry.split(b"\t", 1)
        mode, kind, identity, size = header.decode("ascii").split()
        name = os.fsdecode(name_bytes)
        original, copied = source / name, target / name
        if kind == "commit":
            # Deinitialized submodules stay empty; never fetch their remote sources.
            copied.mkdir(parents=True, exist_ok=True)
            continue
        copied.parent.mkdir(parents=True, exist_ok=True)
        if mode in {"100644", "100755"} and name not in converted:
            try:
                info = original.lstat()
            except FileNotFoundError:
                info = None
            if info is not None and stat.S_ISREG(info.st_mode) and info.st_size == int(size):
                try:
                    copy_file(original, copied)
                except (PermissionError, FileNotFoundError, ValueError):
                    # Source worktree access is optional: the pinned blob remains
                    # authoritative even when the source is unreadable or changing.
                    missing.append(name)
                    continue
                if _blob_matches(copied, identity, int(size), algorithm):
                    copied.chmod(creation_modes[mode])
                    continue
                copied.unlink()  # Dirty source: materialize the pinned Git blob instead.
        missing.append(name)
    # Bounded argv batches; filenames are literal arguments after --, never shell text.
    for start in range(0, len(missing), 32):
        git_process(target, "checkout-index", "--", *missing[start : start + 32])


def local_checkout(source: Path, revision: str, target: Path, branch: str) -> None:
    """Atomically materialize an independent checkout at a local, pinned revision.

    Requires a complete local object database. Partial/alternate-backed repositories
    fail before publication rather than borrowing mutable external object storage.
    Existing destinations are never replaced. Dirty source files are never adopted.
    """
    source, target = source.resolve(), target.resolve()
    if target.exists():
        msg = "Checkout destination already exists"
        raise WorkspaceError(msg)
    resolved = git_process(
        source, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"
    ).stdout.strip()
    pinned = resolved.decode("ascii")
    git_process(source, "check-ref-format", "--branch", branch)
    common = Path(os.fsdecode(git_process(source, "rev-parse", "--git-common-dir").stdout.strip()))
    common = (source / common).resolve() if not common.is_absolute() else common.resolve()
    objects = common / "objects"
    if target.is_relative_to(common) or (objects / "info/alternates").exists():
        msg = "Checkout requires independent local Git object storage"
        raise WorkspaceError(msg)
    if any((objects / "pack").glob("*.promisor")):
        msg = "Partial Git clones cannot materialize an offline independent checkout"
        raise WorkspaceError(msg)
    algorithm = git_process(source, "rev-parse", "--show-object-format").stdout.decode().strip()
    if algorithm not in {"sha1", "sha256"}:
        msg = "Unsupported Git object format"
        raise WorkspaceError(msg)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.preparing-{uuid.uuid4().hex[:12]}")
    staging.mkdir()
    try:
        git_process(staging, "init", "-q", "--template=", f"--object-format={algorithm}")
        _copy_objects(objects, staging / ".git/objects")
        shallow = common / "shallow"
        if shallow.exists():
            copy_file(shallow, staging / ".git/shallow")
        _populate(source, staging, pinned, algorithm)
        git_process(staging, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        git_process(staging, "update-ref", f"refs/heads/{branch}", pinned)
        git_process(staging, "update-index", "--refresh")
        if target.exists():
            msg = "Checkout destination appeared during preparation"
            raise WorkspaceError(msg)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
