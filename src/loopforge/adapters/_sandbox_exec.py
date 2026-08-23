"""Internal POSIX launcher for ConstrainedLocalSandbox.

This module is executed in a dedicated child process. It applies rlimits before replacing itself
with the configured allowlisted executable, avoiding `preexec_fn` in the potentially
threaded parent.
"""

from __future__ import annotations

import os
import resource
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 6 or "--" not in args:
        return 64
    separator = args.index("--")
    if separator != 4 or len(args) <= separator + 1:
        return 64

    cpu_seconds = int(args[0])
    memory_bytes = int(args[1])
    open_files = int(args[2])
    file_bytes = int(args[3])
    target = args[separator + 1 :]

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    os.execve(target[0], target, os.environ)
    return 70  # pragma: no cover - execve replaces the process on success


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess integration tests
    raise SystemExit(main())
