"""Internal POSIX launcher for ConstrainedLocalSandbox.

This module is executed in a dedicated child process. It applies rlimits before replacing itself
with the configured allowlisted executable, avoiding `preexec_fn` in the potentially
threaded parent.

Launcher failures (rlimit rejection, exec failure) exit with LAUNCHER_ERROR_EXIT and a
marker line on stderr so the parent can never confuse them with workload results.
"""

from __future__ import annotations

import os
import resource
import sys

LAUNCHER_ERROR_EXIT: int = 97
LAUNCHER_ERROR_MARKER: str = "loopforge-sandbox-launcher-error:"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 6 or "--" not in args:
        return 64
    separator = args.index("--")
    if separator != 4 or len(args) <= separator + 1:
        return 64

    try:
        cpu_seconds = int(args[0])
        memory_bytes = int(args[1])
        open_files = int(args[2])
        file_bytes = int(args[3])
    except ValueError:
        return 64
    target = args[separator + 1 :]

    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    except (OSError, ValueError) as exc:
        print(f"{LAUNCHER_ERROR_MARKER} resource limits rejected: {exc}", file=sys.stderr)
        return LAUNCHER_ERROR_EXIT
    try:
        os.execve(target[0], target, os.environ)
    except OSError as exc:
        print(f"{LAUNCHER_ERROR_MARKER} exec failed: {exc}", file=sys.stderr)
        return LAUNCHER_ERROR_EXIT
    return 70  # pragma: no cover - execve replaces the process on success


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess integration tests
    raise SystemExit(main())
