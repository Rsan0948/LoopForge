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
import os
import selectors
import subprocess
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

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


@dataclass(frozen=True, slots=True)
class _CapturedOutput:
    stdout: str
    stderr: str
    truncated: bool
    # Infrastructure classification uses captured bytes before display markers.
    stderr_prefix: str


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

    def run(  # noqa: PLR0915 - keep bounded execution and cleanup ordering together
        self, command: str, *, timeout_seconds: int | None = None
    ) -> SandboxCommandResult:
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
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            msg = f"container runtime failed to start: {exc}"
            raise SandboxError(msg) from exc
        began = time.monotonic()
        try:
            captured = self._capture_output(process, limit + _EXEC_GRACE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            self.close()
            msg = f"command outlived its {limit}s bound; the session was destroyed"
            raise SandboxTimeoutError(msg) from exc
        except BaseException as exc:
            with suppress(OSError):
                process.kill()
            process.wait()
            self.close()
            if isinstance(exc, OSError):
                msg = f"container output capture failed: {str(exc)[:300]}"
                raise SandboxError(msg) from exc
            raise
        finally:
            _close_pipes(process)
        # 137 is also any SIGKILL (the memory limit, for one); only a kill at the bound
        # is the timeout's.
        if process.returncode == _KILLED_EXIT and time.monotonic() - began >= limit:
            msg = f"command exceeded {limit}s and was killed; the session continues"
            raise SandboxTimeoutError(msg)
        if process.returncode in {125, 126, 127} and captured.stderr_prefix.startswith(
            ("Error response from daemon", "docker: ")
        ):
            msg = f"container runtime failed: {captured.stderr_prefix}"
            raise SandboxError(msg)
        return SandboxCommandResult(
            exit_code=process.returncode,
            stdout=captured.stdout,
            stderr=captured.stderr,
            succeeded=process.returncode == 0,
            output_truncated=captured.truncated,
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
                    # With --rm, Docker can acknowledge kill before the daemon has
                    # removed the stopped container. Explicit rm makes close()'s
                    # cleanup guarantee synchronous for callers and tests.
                    removed = subprocess.run(
                        [self._config.sandbox.docker_executable, "rm", "--force", name],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=_KILL_TIMEOUT_SECONDS,
                        check=False,
                    )
                    if removed.returncode == 0 or b"No such container" in removed.stderr:
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

    def _capture_output(self, process: subprocess.Popen[bytes], timeout: float) -> _CapturedOutput:
        """Drain both pipes without spooling; retain at most the configured bytes each.

        Excess bytes are discarded while the command continues. Pipe draining stays
        bounded even when descendants keep their inherited handles open after CLI exit.
        This changes capture, not command authority or successful exit semantics.
        """
        assert process.stdout is not None
        assert process.stderr is not None
        streams = (process.stdout, process.stderr)
        buffers = (bytearray(), bytearray())
        truncated = [False, False]
        limit = self._config.sandbox.limits.max_output_bytes
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                for index, stream in enumerate(streams):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, index)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(process.args, timeout)
                    ready = selector.select(min(remaining, 0.05))
                    for key, _ in ready:
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        index = cast("int", key.data)
                        available = limit - len(buffers[index])
                        buffers[index].extend(data[:available])
                        truncated[index] |= len(data) > available
                    if process.poll() is not None:
                        # A writer can exit just after filling a pipe. Drain the
                        # remaining prefix without waiting for inherited handles.
                        # Stop after cap+1 bytes even if a descendant keeps writing.
                        for pending in list(selector.get_map().values()):
                            index = cast("int", pending.data)
                            if not truncated[index]:
                                truncated[index] = _drain_exited_pipe(
                                    pending.fd, buffers[index], limit
                                )
                        break
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
        finally:
            _close_pipes(process)
        stdout, stdout_cut = _render_output(buffers[0], truncated[0], limit)
        stderr, stderr_cut = _render_output(buffers[1], truncated[1], limit)
        return _CapturedOutput(
            stdout,
            stderr,
            stdout_cut or stderr_cut,
            bytes(buffers[1][:300]).decode("utf-8", errors="replace"),
        )


def _drain_exited_pipe(fd: int, buffer: bytearray, limit: int) -> bool:
    """Drain an exited command's available prefix; never wait on descendants."""
    while True:
        available = limit - len(buffer)
        try:
            tail = os.read(fd, min(65536, available + 1))
        except BlockingIOError:
            return False
        if not tail:
            return False
        buffer.extend(tail[:available])
        if len(tail) > available:
            return True


def _render_output(data: bytearray, overflow: bool, limit: int) -> tuple[str, bool]:
    """Bound UTF-8 text bytes after replacement, including the truncation marker."""
    decoded = data.decode("utf-8", errors="replace")
    encoded = decoded.encode("utf-8")
    overflow = overflow or len(encoded) > limit
    if not overflow:
        return decoded, False
    marker = b"\n[output truncated]"[:limit]
    prefix = encoded[: limit - len(marker)].decode("utf-8", errors="ignore")
    return prefix + marker.decode("utf-8"), True


def _close_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
