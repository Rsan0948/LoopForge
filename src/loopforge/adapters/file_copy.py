"""Independent file copies, using filesystem copy-on-write when available.

Reflinks share physical blocks, never mutable inodes or host path dependencies.
This is a storage optimization, not an isolation capability.
"""

import ctypes
import errno
import os
import shutil
import stat
import sys
from pathlib import Path

_UNSUPPORTED = {errno.EXDEV, errno.ENOSYS, errno.ENOTSUP, errno.EINVAL, errno.ENOTTY}


def _directory_fd(path: Path) -> int:
    """Pin each directory without following symlinks, including ancestor swaps."""
    absolute = path.absolute()
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    else:
        return descriptor


def _clone_file(source: int, parent: int, name: str) -> None:
    """Create an exclusive reflink; failure leaves no Linux target behind."""
    if sys.platform == "darwin":
        signature = ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
            use_errno=True,
        )
        clone = signature(("fclonefileat", ctypes.CDLL(None, use_errno=True)))
        # CLONE_NOOWNERCOPY: data/mode remain independent, owner remains this user.
        if clone(source, parent, os.fsencode(name), 2) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), name)
        return
    if sys.platform == "linux":
        import fcntl  # noqa: PLC0415 - platform-specific adapter

        target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            # Linux UAPI FICLONE: reflink the whole file into a distinct inode.
            fcntl.ioctl(target, 0x40049409, source)
        except BaseException:
            os.unlink(name, dir_fd=parent)
            raise
        finally:
            os.close(target)
        return
    raise OSError(errno.ENOTSUP, "Filesystem cloning is unavailable")


def _copy_open_file(source: int, parent: int, name: str, *, require_clone: bool) -> bool:
    info = os.fstat(source)
    if not stat.S_ISREG(info.st_mode):
        msg = "File copies require a regular, non-symlink source"
        raise ValueError(msg)
    created = False
    try:
        cloned = True
        try:
            _clone_file(source, parent, name)
            created = True
        except OSError as exc:
            if require_clone or exc.errno not in _UNSUPPORTED:
                raise
            cloned = False
            target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            created = True
            with os.fdopen(target, "wb") as copied, os.fdopen(os.dup(source), "rb") as original:
                shutil.copyfileobj(original, copied)
        target = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            os.fchmod(target, stat.S_IMODE(info.st_mode))
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
        finally:
            os.close(target)
    except BaseException:
        if created:
            os.unlink(name, dir_fd=parent)
        raise
    else:
        return cloned


def copy_file(source: Path, target: Path, *, require_clone: bool = False) -> bool:
    """Copy a regular file to a new path, retaining mode/timestamps; return if cloned.

    Never hardlink, overwrite or follow symlinks. Unsupported cloning falls back to
    copying unless require_clone is set. Real failures (including a full disk) are
    not hidden by an attempted second copy. Source and target parents must exist.
    """
    original_parent = _directory_fd(source.parent)
    try:
        original = os.open(
            source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=original_parent
        )
        try:
            parent = _directory_fd(target.parent)
            try:
                return _copy_open_file(original, parent, target.name, require_clone=require_clone)
            finally:
                os.close(parent)
        finally:
            os.close(original)
    finally:
        os.close(original_parent)


def copy_metadata(source: Path, target: Path) -> None:
    """Preserve an existing file's metadata when replacing only its data storage.

    Paths are resolved through pinned, no-follow descriptors. On macOS fcopyfile
    preserves ACLs, stat metadata and xattrs without copying data. Linux ACLs are
    represented by xattrs. Any unsupported metadata operation fails to the caller;
    archive compaction then leaves the original file in place.
    """
    source_parent = _directory_fd(source.parent)
    try:
        original = os.open(
            source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent
        )
        try:
            target_parent = _directory_fd(target.parent)
            try:
                copied = os.open(
                    target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=target_parent
                )
                try:
                    _copy_metadata(original, copied)
                finally:
                    os.close(copied)
            finally:
                os.close(target_parent)
        finally:
            os.close(original)
    finally:
        os.close(source_parent)


def _copy_metadata(source: int, target: int) -> None:
    info, current = os.fstat(source), os.fstat(target)
    if not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(current.st_mode):
        msg = "Metadata copies require regular files"
        raise ValueError(msg)
    if sys.platform == "darwin":
        signature = ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            use_errno=True,
        )
        copy = signature(("fcopyfile", ctypes.CDLL(None, use_errno=True)))
        # COPYFILE_METADATA = COPYFILE_ACL | COPYFILE_STAT | COPYFILE_XATTR.
        os.fchmod(target, stat.S_IMODE(current.st_mode) | stat.S_IWUSR)
        if copy(source, target, None, 7) != 0:
            code = ctypes.get_errno()
            os.fchmod(target, stat.S_IMODE(current.st_mode))
            raise OSError(code, os.strerror(code))
        return
    if (info.st_uid, info.st_gid) != (current.st_uid, current.st_gid):
        os.fchown(target, info.st_uid, info.st_gid)
    os.fchmod(target, stat.S_IMODE(current.st_mode) | stat.S_IWUSR)
    try:
        wanted = set(os.listxattr(source))
        for name in set(os.listxattr(target)) - wanted:
            os.removexattr(target, name)
        for name in wanted:
            os.setxattr(target, name, os.getxattr(source, name))
    except BaseException:
        os.fchmod(target, stat.S_IMODE(current.st_mode))
        raise
    os.fchmod(target, stat.S_IMODE(info.st_mode))
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
