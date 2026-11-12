"""Storage sharing must preserve independent files and fail safely."""

import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from loopforge.adapters import file_copy


def test_real_copy_preserves_metadata_and_independent_mutation(tmp_path: Path) -> None:
    original, copied = tmp_path / "original", tmp_path / "copied"
    original.write_bytes(b"payload" * 20000)
    original.chmod(0o755)
    os.utime(original, ns=(1_000_000_000, 2_000_000_000))
    file_copy.copy_file(original, copied)
    assert copied.read_bytes() == original.read_bytes()
    assert copied.stat().st_ino != original.stat().st_ino
    assert stat.S_IMODE(copied.stat().st_mode) == 0o755
    assert copied.stat().st_mtime_ns == 2_000_000_000
    copied.write_bytes(b"candidate mutation")
    assert original.read_bytes() == b"payload" * 20000
    original.unlink()
    assert copied.read_bytes() == b"candidate mutation"


def test_unsupported_cloning_falls_back_to_an_independent_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(_source: int, _parent: int, _name: str) -> None:
        raise OSError(errno.EXDEV, "different filesystems")

    monkeypatch.setattr(file_copy, "_clone_file", unsupported)
    original, copied = tmp_path / "original", tmp_path / "copied"
    original.write_bytes(b"fallback bytes")
    assert not file_copy.copy_file(original, copied)
    copied.write_bytes(b"different")
    assert original.read_bytes() == b"fallback bytes"
    with pytest.raises(OSError, match="different filesystems"):
        file_copy.copy_file(original, tmp_path / "required", require_clone=True)
    assert not (tmp_path / "required").exists()


def test_real_disk_errors_do_not_trigger_another_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exhausted(_source: int, _parent: int, _name: str) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(file_copy, "_clone_file", exhausted)
    original, copied = tmp_path / "original", tmp_path / "copied"
    original.write_bytes(b"original")
    with pytest.raises(OSError, match="disk full"):
        file_copy.copy_file(original, copied)
    assert not copied.exists()
    assert original.read_bytes() == b"original"


def test_copy_never_overwrites_a_destination_or_follows_symlinks(tmp_path: Path) -> None:
    original, copied = tmp_path / "original", tmp_path / "copied"
    original.write_bytes(b"original")
    copied.write_bytes(b"retained")
    with pytest.raises(FileExistsError):
        file_copy.copy_file(original, copied)
    assert copied.read_bytes() == b"retained"
    link = tmp_path / "link"
    link.symlink_to(original)
    with pytest.raises(OSError, match=r"Too many levels|Not a directory"):
        file_copy.copy_file(link, tmp_path / "escaped")
    directory = tmp_path / "directory"
    directory.mkdir()
    ancestor = tmp_path / "ancestor"
    ancestor.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError, match=r"Too many levels|Not a directory"):
        file_copy.copy_file(original, ancestor / "escaped")
    assert not (directory / "escaped").exists()


def test_special_files_are_refused_without_waiting_for_a_writer(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        file_copy.copy_file(fifo, tmp_path / "never-created")
    assert not (tmp_path / "never-created").exists()


def test_metadata_copy_keeps_data_and_restores_xattrs_mode_and_time(tmp_path: Path) -> None:
    original, copied = tmp_path / "metadata", tmp_path / "data"
    original.write_bytes(b"metadata source")
    copied.write_bytes(b"different preserved payload")
    if sys.platform == "darwin":
        for path, name, value in (
            (original, "user.loopforge-test", "expected"),
            (copied, "user.loopforge-extra", "remove"),
        ):
            subprocess.run(["/usr/bin/xattr", "-w", name, value, str(path)], check=True, timeout=5)
    else:
        os.setxattr(original, "user.loopforge-test", b"expected")
        os.setxattr(copied, "user.loopforge-extra", b"remove")
    original.chmod(0o444)
    copied.chmod(0o444)
    os.utime(original, ns=(1_000_000_000, 2_000_000_000))
    file_copy.copy_metadata(original, copied)
    assert copied.read_bytes() == b"different preserved payload"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o444
    assert copied.stat().st_mtime_ns == 2_000_000_000
    if sys.platform == "darwin":
        read = subprocess.run(
            ["/usr/bin/xattr", "-p", "user.loopforge-test", str(copied)],
            check=True,
            capture_output=True,
            timeout=5,
        )
        assert read.stdout.strip() == b"expected"
        names = subprocess.run(
            ["/usr/bin/xattr", str(copied)], check=True, capture_output=True, timeout=5
        )
        assert b"user.loopforge-extra" not in names.stdout.splitlines()
    else:
        assert os.getxattr(copied, "user.loopforge-test") == b"expected"
        assert "user.loopforge-extra" not in os.listxattr(copied)
