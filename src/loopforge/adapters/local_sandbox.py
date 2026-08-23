from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Mapping

from loopforge.domain.security import SandboxCapabilities
from loopforge.ports.sandbox import (
    SandboxCommandResult,
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
        if min(values) <= 0:
            raise ValueError("sandbox limits must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    cpu_seconds: int = 10
    allowed_exit_codes: frozenset[int] = frozenset({0})

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.argv:
            raise ValueError("command name and argv are required")
        if not Path(self.argv[0]).is_absolute():
            raise ValueError("sandbox command executable must be an absolute path")
        if self.timeout_seconds <= 0 or self.cpu_seconds <= 0:
            raise ValueError("command time limits must be positive")
        if not self.allowed_exit_codes:
            raise ValueError("allowed_exit_codes cannot be empty")


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

    def __init__(
        self,
        root: str | Path,
        *,
        commands: list[CommandSpec],
        environment: Mapping[str, str] | None = None,
        limits: SandboxLimits = SandboxLimits(),
    ) -> None:
        try:
            root_path = Path(root).resolve(strict=True)
        except FileNotFoundError as exc:
            raise SandboxPathError("sandbox root must exist") from exc
        if not root_path.is_dir():
            raise SandboxPathError("sandbox root must be an existing directory")
        self._root = root_path
        self._commands = MappingProxyType({item.name: item for item in commands})
        if len(self._commands) != len(commands):
            raise ValueError("sandbox command names must be unique")
        self._environment = MappingProxyType(dict(environment or {}))
        self._limits = limits

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self._CAPABILITIES

    def read_text(self, relative_path: str) -> str:
        path = self._safe_path(relative_path, allow_missing=False)
        if not path.is_file():
            raise SandboxPathError("read target must be a regular file")
        if path.stat().st_size > self._limits.max_read_bytes:
            raise SandboxPolicyError("read exceeds sandbox byte limit")
        return path.read_text(encoding="utf-8")

    def write_text(self, relative_path: str, content: str) -> None:
        encoded = content.encode("utf-8")
        if len(encoded) > self._limits.max_write_bytes:
            raise SandboxPolicyError("write exceeds sandbox byte limit")
        path = self._safe_path(relative_path, allow_missing=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink_components(path.parent)
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise SandboxPathError("write target must be a regular non-symlink file")
        fd, temp_name = tempfile.mkstemp(prefix=".loopforge-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        try:
            spec = self._commands[command_name]
        except KeyError as exc:
            raise SandboxPolicyError(f"command is not allowlisted: {command_name}") from exc

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
            effective_timeout = spec.timeout_seconds
            if timeout_seconds is not None:
                if timeout_seconds <= 0:
                    raise SandboxPolicyError("runtime timeout must be positive")
                effective_timeout = min(effective_timeout, timeout_seconds)
            try:
                process.wait(timeout=effective_timeout)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise SandboxTimeoutError(
                    f"command {command_name!r} exceeded {effective_timeout}s"
                ) from exc

            stdout = self._read_output(stdout_file)
            stderr = self._read_output(stderr_file)
            return SandboxCommandResult(
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                succeeded=process.returncode in spec.allowed_exit_codes,
            )

    def _safe_path(self, relative_path: str, *, allow_missing: bool) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or ".." in raw.parts:
            raise SandboxPathError("sandbox paths must be relative and cannot contain '..'")
        candidate = self._root.joinpath(raw)
        self._assert_no_symlink_components(candidate.parent if allow_missing else candidate)
        if allow_missing and candidate.is_symlink():
            raise SandboxPathError("symlink targets are not allowed in sandbox paths")
        try:
            resolved = candidate.resolve(strict=not allow_missing)
        except FileNotFoundError as exc:
            raise SandboxPathError("sandbox path does not exist") from exc
        if not resolved.is_relative_to(self._root):
            raise SandboxPathError("sandbox path escapes configured root")
        return resolved

    def _assert_no_symlink_components(self, path: Path) -> None:
        current = self._root
        try:
            relative = path.relative_to(self._root)
        except ValueError as exc:
            raise SandboxPathError("sandbox path escapes configured root") from exc
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise SandboxPathError("symlink components are not allowed in sandbox paths")

    def _read_output(self, handle: BinaryIO) -> str:
        handle.seek(0)
        data = handle.read(self._limits.max_output_bytes + 1)
        if len(data) > self._limits.max_output_bytes:
            data = data[: self._limits.max_output_bytes]
        return data.decode("utf-8", errors="replace")
