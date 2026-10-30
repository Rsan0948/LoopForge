"""A persistent shell session inside one hardened container.

``ContainerSandbox`` runs each allowlisted command in a fresh container. A session keeps
one container alive across calls, so a started server, a background process or a file
under ``/tmp`` survives from one command to the next: an agent can start an application
once and then drive it, watch its log and query it.

Isolation is exactly ``ContainerSandbox``'s, built by the same ``hardened_run_argv``:
deny-all network (only the container's own loopback), read-only root, size-bounded
``noexec`` ``/tmp``, all capabilities dropped, no new privileges, pid/memory/open-file
limits, the code-owned environment only, and the configured workspace mount. The session
adds no authority beyond a one-shot command: a command string runs inside that boundary
and can only do what a script run there could do (AGENTS.md rules 13-15). What it adds is
time, so time is bounded twice:

- every command runs under the image's ``timeout`` with SIGKILL, and the host abandons
  and destroys the whole session if the CLI outlives that bound;
- the container's own keepalive process is ``sleep <max_session_seconds>``, so a session
  whose host process died still stops on its own and ``--rm`` removes it.

``kernel_isolated`` stays ``False`` for the same reason as ``ContainerSandbox``.
"""

from __future__ import annotations

import math
import subprocess
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from loopforge.adapters.container_sandbox import (
    ContainerSandbox,
    ContainerSandboxConfig,
    hardened_run_argv,
)
from loopforge.domain.security import SandboxCapabilities
from loopforge.ports.sandbox import (
    SandboxCommandResult,
    SandboxError,
    SandboxPolicyError,
    SandboxTimeoutError,
)

_SESSION_NAME_PREFIX: Final = "loopforge-session-"
_START_TIMEOUT_SECONDS: Final = 60.0
_KILL_TIMEOUT_SECONDS: Final = 10.0
# The CLI is given this much beyond the in-container timeout before the host gives up.
_EXEC_GRACE_SECONDS: Final = 15.0
# `timeout -s KILL` exits 137 when it had to kill the command.
_KILLED_EXIT: Final = 137


@dataclass(frozen=True, slots=True, kw_only=True)
class ContainerSessionConfig:
    """Code-owned session limits; repository or model content can never raise them."""

    sandbox: ContainerSandboxConfig
    command_timeout_seconds: int = 120
    max_session_seconds: int = 3600
    max_command_chars: int = 8000
    shell: tuple[str, ...] = ("/bin/sh", "-c")

    def __post_init__(self) -> None:
        if (
            type(self.command_timeout_seconds) is not int
            or type(self.max_session_seconds) is not int
            or type(self.max_command_chars) is not int
            or not 0 < self.command_timeout_seconds <= self.max_session_seconds
            or self.max_command_chars <= 0
        ):
            msg = "session limits must be positive integers, a command within the session"
            raise ValueError(msg)
        if not self.shell or any(not part or part.startswith("-") for part in self.shell[:1]):
            msg = "session shell must be an executable path"
            raise ValueError(msg)


class ContainerSession:
    """One long-lived hardened container, started lazily and destroyed on close."""

    def __init__(self, root: str | Path, *, config: ContainerSessionConfig) -> None:
        # The sandbox validates the root and file API exactly as for one-shot commands.
        self._sandbox = ContainerSandbox(root, config=config.sandbox)
        self._root = Path(root).resolve()
        self._config = config
        self._name: str | None = None
        self._started_at: float | None = None
        self._closed = False

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self._sandbox.capabilities

    @property
    def active_container(self) -> str | None:
        return self._name

    def run(self, command: str, *, timeout_seconds: int | None = None) -> SandboxCommandResult:
        """Run one shell command in the session; its processes may outlive the call."""
        if self._closed:
            msg = "session is closed"
            raise SandboxPolicyError(msg)
        if not command.strip() or len(command) > self._config.max_command_chars or "\0" in command:
            msg = f"command must be 1-{self._config.max_command_chars} characters without NUL"
            raise SandboxPolicyError(msg)
        limit = self._config.command_timeout_seconds
        if timeout_seconds is not None:
            if type(timeout_seconds) is not int or not timeout_seconds > 0:
                msg = "runtime timeout must be a positive integer"
                raise SandboxPolicyError(msg)
            limit = min(limit, timeout_seconds)
        name = self._ensure_started()
        assert self._started_at is not None
        remaining = self._config.max_session_seconds - (time.monotonic() - self._started_at)
        if remaining < 1:
            self.close()
            msg = f"session exceeded {self._config.max_session_seconds}s and was destroyed"
            raise SandboxTimeoutError(msg)
        limit = min(limit, math.floor(remaining))
        argv = (
            self._config.sandbox.docker_executable,
            "exec",
            name,
            "timeout",
            "-s",
            "KILL",
            str(limit),
            *self._config.shell,
            command,
        )
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                msg = f"container runtime failed to start: {exc}"
                raise SandboxError(msg) from exc
            began = time.monotonic()
            try:
                process.wait(timeout=limit + _EXEC_GRACE_SECONDS)
            except subprocess.TimeoutExpired as exc:
                # The in-container bound did not end the command: nothing in the session
                # can be trusted to stop, so the whole session goes.
                process.kill()
                process.wait()
                self.close()
                msg = f"command outlived its {limit}s bound; the session was destroyed"
                raise SandboxTimeoutError(msg) from exc
            stdout = self._read_output(stdout_file)
            stderr = self._read_output(stderr_file)
        # 137 is also any SIGKILL (the memory limit, for one); only a kill at the bound
        # is the timeout's.
        if process.returncode == _KILLED_EXIT and time.monotonic() - began >= limit:
            msg = f"command exceeded {limit}s and was killed; the session continues"
            raise SandboxTimeoutError(msg)
        if process.returncode in {125, 126, 127} and stderr.startswith(
            ("Error response from daemon", "docker: ")
        ):
            msg = f"container runtime failed: {stderr[:300]}"
            raise SandboxError(msg)
        return SandboxCommandResult(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            succeeded=process.returncode == 0,
        )

    def close(self) -> None:
        """Kill the session container; every process in it ends with its PID namespace."""
        self._closed = True
        name, self._name = self._name, None
        if name is None:
            return
        deadline = time.monotonic() + _KILL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with suppress(OSError, subprocess.TimeoutExpired):
                done = subprocess.run(
                    [self._config.sandbox.docker_executable, "kill", name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=_KILL_TIMEOUT_SECONDS,
                    check=False,
                )
                # Killed, or already gone (its keepalive ended and --rm removed it).
                if done.returncode == 0 or b"No such container" in done.stderr:
                    return
            time.sleep(0.1)

    def __enter__(self) -> ContainerSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _ensure_started(self) -> str:
        if self._name is not None:
            return self._name
        name = f"{_SESSION_NAME_PREFIX}{uuid.uuid4().hex[:12]}"
        argv = (
            *hardened_run_argv(
                self._config.sandbox,
                self._root,
                container_name=name,
                cpu_seconds=self._config.max_session_seconds,
                detached=True,
            ),
            "sleep",
            str(self._config.max_session_seconds),
        )
        try:
            started = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_START_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # A start that timed out may still come up; it stops by its keepalive bound.
            msg = f"session container failed to start: {type(exc).__name__}"
            raise SandboxError(msg) from exc
        if started.returncode != 0:
            msg = f"session container failed to start: {started.stderr.decode()[:300]}"
            raise SandboxError(msg)
        self._name = name
        self._started_at = time.monotonic()
        return name

    def _read_output(self, handle: BinaryIO) -> str:
        limit = self._config.sandbox.limits.max_output_bytes
        handle.seek(0)
        data = handle.read(limit + 1)[:limit]
        return data.decode("utf-8", errors="replace")
