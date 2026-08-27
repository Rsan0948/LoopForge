from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Final

from loopforge.adapters._sandbox_exec import LAUNCHER_ERROR_EXIT, LAUNCHER_ERROR_MARKER
from loopforge.domain.security import SandboxCapabilities
from loopforge.ports.sandbox import (
    SandboxCommandResult,
    SandboxError,
    SandboxPathError,
    SandboxPolicyError,
    SandboxTimeoutError,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxLimits:
    max_read_bytes: int = 1_000_000
    max_write_bytes: int = 1_000_000
    max_output_bytes: int = 1_000_000
    max_memory_bytes: int = 512 * 1024 * 1024
    max_open_files: int = 128

    def __post_init__(self) -> None:
        values = (
            self.max_read_bytes,
            self.max_write_bytes,
            self.max_output_bytes,
            self.max_memory_bytes,
            self.max_open_files,
        )
        if not all(map(math.isfinite, values)) or min(values) <= 0:
            msg = "sandbox limits must be positive and finite"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    cpu_seconds: int = 10
    allowed_exit_codes: frozenset[int] = frozenset({0})

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.argv:
            msg_2 = "command name and argv are required"
            raise ValueError(msg_2)
        if not Path(self.argv[0]).is_absolute():
            msg_3 = "sandbox command executable must be an absolute path"
            raise ValueError(msg_3)
        if (
            not math.isfinite(self.timeout_seconds)
            or not math.isfinite(self.cpu_seconds)
            or self.timeout_seconds <= 0
            or self.cpu_seconds <= 0
        ):
            msg_4 = "command time limits must be positive and finite"
            raise ValueError(msg_4)
        if not self.allowed_exit_codes:
            msg_5 = "allowed_exit_codes cannot be empty"
            raise ValueError(msg_5)


class ConstrainedLocalSandbox:
    """Constrained local process/workspace adapter.

    This adapter deliberately does not claim network or kernel isolation. It is useful for trusted
    developer workloads and contract testing; hostile-code execution requires a later container/VM
    adapter whose capability flags truthfully report stronger isolation.
    """

    _CAPABILITIES = SandboxCapabilities(
        file_api_confined=True,
        symlink_protected=True,
        environment_filtered=True,
        process_timeout=True,
        resource_limits=True,
        output_limited=True,
        process_filesystem_isolated=False,
        network_isolated=False,
        kernel_isolated=False,
    )

    _DEFAULT_LIMITS: Final = SandboxLimits()

    def __init__(
        self,
        root: str | Path,
        *,
        commands: list[CommandSpec],
        environment: Mapping[str, str] | None = None,
        limits: SandboxLimits = _DEFAULT_LIMITS,
    ) -> None:
        try:
            root_path = Path(root).resolve(strict=True)
        except FileNotFoundError as exc:
            msg_15 = "sandbox root must exist"
            raise SandboxPathError(msg_15) from exc
        if not root_path.is_dir():
            msg_6 = "sandbox root must be an existing directory"
            raise SandboxPathError(msg_6)
        self._root = root_path
        self._commands = MappingProxyType({item.name: item for item in commands})
        if len(self._commands) != len(commands):
            msg_7 = "sandbox command names must be unique"
            raise ValueError(msg_7)
        self._environment = MappingProxyType(dict(environment or {}))
        self._limits = limits

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self._CAPABILITIES

    @property
    def root(self) -> Path:
        """Resolved workspace root this sandbox is confined to."""
        return self._root

    def read_text(self, relative_path: str) -> str:
        path = self._safe_path(relative_path, allow_missing=False)
        if not path.is_file():
            msg_8 = "read target must be a regular file"
            raise SandboxPathError(msg_8)
        if path.stat().st_size > self._limits.max_read_bytes:
            msg_9 = "read exceeds sandbox byte limit"
            raise SandboxPolicyError(msg_9)
        return path.read_text(encoding="utf-8")

    def write_text(self, relative_path: str, content: str) -> None:
        encoded = content.encode("utf-8")
        if len(encoded) > self._limits.max_write_bytes:
            msg_10 = "write exceeds sandbox byte limit"
            raise SandboxPolicyError(msg_10)
        path = self._safe_path(relative_path, allow_missing=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink_components(path.parent)
        if path.exists() and (path.is_symlink() or not path.is_file()):
            msg_11 = "write target must be a regular non-symlink file"
            raise SandboxPathError(msg_11)
        fd, temp_name = tempfile.mkstemp(prefix=".loopforge-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            Path(temp_name).replace(path)
        finally:
            with suppress(FileNotFoundError):
                Path(temp_name).unlink()

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        try:
            spec = self._commands[command_name]
        except KeyError as exc:
            msg_16 = f"command is not allowlisted: {command_name}"
            raise SandboxPolicyError(msg_16) from exc
        effective_timeout = spec.timeout_seconds
        if timeout_seconds is not None:
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                msg_20 = "runtime timeout must be positive and finite"
                raise SandboxPolicyError(msg_20)
            effective_timeout = min(effective_timeout, timeout_seconds)

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            launcher = Path(__file__).with_name("_sandbox_exec.py")
            argv = (
                sys.executable,
                str(launcher),
                str(spec.cpu_seconds),
                str(self._limits.max_memory_bytes),
                str(self._limits.max_open_files),
                str(self._limits.max_output_bytes),
                "--",
                *spec.argv,
            )
            process = subprocess.Popen(
                argv,
                cwd=self._root,
                env=dict(self._environment),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
            try:
                process.wait(timeout=effective_timeout)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                msg_21 = f"command {command_name!r} exceeded {effective_timeout}s"
                raise SandboxTimeoutError(msg_21) from exc

            stdout = self._read_output(stdout_file)
            stderr = self._read_output(stderr_file)
            if process.returncode == LAUNCHER_ERROR_EXIT and stderr.startswith(
                LAUNCHER_ERROR_MARKER
            ):
                msg_22 = f"sandbox launcher failed: {stderr}"
                raise SandboxError(msg_22)
            return SandboxCommandResult(
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                succeeded=process.returncode in spec.allowed_exit_codes,
            )

    def _safe_path(self, relative_path: str, *, allow_missing: bool) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or ".." in raw.parts:
            msg_12 = "sandbox paths must be relative and cannot contain '..'"
            raise SandboxPathError(msg_12)
        candidate = self._root.joinpath(raw)
        self._assert_no_symlink_components(candidate.parent if allow_missing else candidate)
        if allow_missing and candidate.is_symlink():
            msg_13 = "symlink targets are not allowed in sandbox paths"
            raise SandboxPathError(msg_13)
        try:
            resolved = candidate.resolve(strict=not allow_missing)
        except FileNotFoundError as exc:
            msg_17 = "sandbox path does not exist"
            raise SandboxPathError(msg_17) from exc
        if not resolved.is_relative_to(self._root):
            msg_14 = "sandbox path escapes configured root"
            raise SandboxPathError(msg_14)
        return resolved

    def _assert_no_symlink_components(self, path: Path) -> None:
        current = self._root
        try:
            relative = path.relative_to(self._root)
        except ValueError as exc:
            msg_18 = "sandbox path escapes configured root"
            raise SandboxPathError(msg_18) from exc
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                msg_19 = "symlink components are not allowed in sandbox paths"
                raise SandboxPathError(msg_19)

    def _read_output(self, handle: BinaryIO) -> str:
        handle.seek(0)
        data = handle.read(self._limits.max_output_bytes + 1)
        if len(data) > self._limits.max_output_bytes:
            data = data[: self._limits.max_output_bytes]
        return data.decode("utf-8", errors="replace")
