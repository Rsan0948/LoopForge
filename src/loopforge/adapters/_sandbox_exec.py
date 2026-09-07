"""Internal POSIX launcher for ConstrainedLocalSandbox.

This module is executed in a dedicated child process. It applies rlimits before replacing itself
with the configured allowlisted executable, avoiding `preexec_fn` in the potentially
threaded parent.

Resource limits are applied individually: platforms that reject a specific limit (macOS rejects
RLIMIT_AS outright) skip ONLY that limit and still run the command — failing closed there would
make every trusted-local check impossible on an entire OS. The launcher always prints exactly
one status line as the FIRST line of stderr (`LAUNCHER_LIMITS_MARKER`) naming any skipped
limits, so the parent can record the degradation honestly and strip the line before workload
output is observed. Because the launcher line is always first, workload output can never spoof
it.

Exec failures exit with LAUNCHER_ERROR_EXIT and LAUNCHER_ERROR_MARKER on stderr so the parent
can never confuse them with workload results.
"""

from __future__ import annotations

import os
import resource
import sys

LAUNCHER_ERROR_EXIT: int = 97
LAUNCHER_ERROR_MARKER: str = "loopforge-sandbox-launcher-error:"
LAUNCHER_LIMITS_MARKER: str = "loopforge-sandbox-launcher-limits:"


def _apply_limits(
    cpu_seconds: int, memory_bytes: int, open_files: int, file_bytes: int
) -> list[str]:
    """Apply each rlimit independently; return the names of any the platform rejected."""
    requested = (
        ("RLIMIT_CPU", resource.RLIMIT_CPU, cpu_seconds),
        ("RLIMIT_AS", resource.RLIMIT_AS, memory_bytes),
        ("RLIMIT_NOFILE", resource.RLIMIT_NOFILE, open_files),
        ("RLIMIT_FSIZE", resource.RLIMIT_FSIZE, file_bytes),
    )
    skipped: list[str] = []
    for name, limit_resource, value in requested:
        try:
            resource.setrlimit(limit_resource, (value, value))
        except (OSError, ValueError):
            skipped.append(name)
    return skipped


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

    skipped = _apply_limits(cpu_seconds, memory_bytes, open_files, file_bytes)
    skipped_text = ",".join(skipped) if skipped else "none"
    print(f"{LAUNCHER_LIMITS_MARKER} skipped={skipped_text}", file=sys.stderr, flush=True)
    try:
        os.execve(target[0], target, os.environ)
    except OSError as exc:
        print(f"{LAUNCHER_ERROR_MARKER} exec failed: {exc}", file=sys.stderr)
        return LAUNCHER_ERROR_EXIT
    return 70  # pragma: no cover - execve replaces the process on success


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess integration tests
    raise SystemExit(main())
