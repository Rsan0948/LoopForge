"""Container-backed sandbox adapter driven through the Docker CLI.

This adapter executes allowlisted commands inside hardened containers so untrusted
repository/build workloads can run with namespace isolation that
``ConstrainedLocalSandbox`` deliberately does not claim.

Enforced isolation (truthfully advertised in ``capabilities``):

- mount namespace: only the configured workspace is bind-mounted, at ``/workspace``;
  the container root filesystem is read-only and ``/tmp`` is a size-bounded tmpfs;
- network namespace: the only supported network policy is deny-all (``--network none``);
- process isolation: the workload runs in its own PID namespace; killing the named
  container destroys every process inside it;
- hardening flags: ``--cap-drop ALL``, ``--security-opt no-new-privileges``,
  ``--init``, ``--pull never`` (no implicit registry access), ``--log-driver none``;
- resource limits: memory (+swap pinned equal), pids, CPU seconds, and open files via
  container/ulimit enforcement; captured stdout/stderr are bounded by the adapter;
- environment filtering: only the explicit code-owned environment mapping is passed
  via ``--env``; the host process environment is never inherited (the image's own
  minimal ENV entries still apply and are part of image trust).

Explicitly not claimed (AGENTS.md rules 13-15):

- ``kernel_isolated`` stays ``False``. Containers share a kernel with the runtime
  host; on Docker Desktop the Linux VM is an implementation detail of the runtime,
  not a contract guarantee this adapter enforces.

Image trust, the container runtime, and the Docker executable path are operator/config
owned. Repository or model content can never select the image, extend the command
allowlist, widen the environment, or relax the network policy (rule 14).

The Docker CLI is used instead of an SDK so the adapter adds zero dependencies and its
exact enforcement surface stays an auditable argv. File API operations are delegated to
an internal ``ConstrainedLocalSandbox`` so the traversal/symlink/byte-limit defenses
from PACS-005 are retained unchanged.
"""

from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Final

from loopforge.adapters.local_sandbox import (
    CommandSpec,
    ConstrainedLocalSandbox,
    SandboxLimits,
)
from loopforge.domain.security import SandboxCapabilities
from loopforge.ports.sandbox import (
    SandboxCommandResult,
    SandboxError,
    SandboxPathError,
    SandboxPolicyError,
    SandboxTimeoutError,
)


class ContainerNetworkPolicy(StrEnum):
    """Code-owned network policy for container workloads.

    Only the deny-all policy is implemented. An egress allowlist/proxy policy is a
    future extension and must never be selectable by repository or model content.
    """

    NONE = "none"


_WORKSPACE_MOUNT: Final = "/workspace"
_DOCKER_CLI_ERROR_EXIT: Final = 125
_DOCKER_CLI_ERROR_MARKER: Final = "docker: "
_KILL_TIMEOUT_SECONDS: Final = 10.0
_KILL_POLL_INTERVAL_SECONDS: Final = 0.1
_CONTAINER_NAME_PREFIX: Final = "loopforge-sandbox-"
_ENV_KEY_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEFAULT_LIMITS: Final = SandboxLimits()


@dataclass(frozen=True, slots=True, kw_only=True)
class ContainerSandboxConfig:
    """Code-owned container configuration; invalid configs are unconstructable.

    Every field is bootstrap/operator authority: repository or model content can never
    select the image, extend the command allowlist, widen the environment, relax the
    network policy, or raise the resource ceilings (AGENTS.md rule 14).
    """

    image: str
    commands: tuple[CommandSpec, ...]
    environment: Mapping[str, str] | None = None
    limits: SandboxLimits = _DEFAULT_LIMITS
    network: ContainerNetworkPolicy = ContainerNetworkPolicy.NONE
    pids_limit: int = 128
    tmpfs_bytes: int = 64 * 1024 * 1024
    user: str | None = None
    docker_executable: str = "docker"

    def __post_init__(self) -> None:
        if not self.image.strip() or any(char.isspace() for char in self.image):
            msg = "container image must be a non-empty reference without whitespace"
            raise ValueError(msg)
        if (
            not math.isfinite(self.pids_limit)
            or not math.isfinite(self.tmpfs_bytes)
            or self.pids_limit <= 0
            or self.tmpfs_bytes <= 0
        ):
            msg_2 = "container pids/tmpfs limits must be positive and finite"
            raise ValueError(msg_2)
        if self.user is not None and (
            not self.user.strip() or any(char.isspace() for char in self.user)
        ):
            msg_3 = "container user must be non-empty without whitespace"
            raise ValueError(msg_3)
        if not self.docker_executable.strip():
            msg_4 = "docker executable must be non-empty"
            raise ValueError(msg_4)
        for key in self.environment or {}:
            if _ENV_KEY_PATTERN.fullmatch(key) is None:
                msg_5 = f"invalid container environment variable name: {key!r}"
                raise ValueError(msg_5)
        names = [item.name for item in self.commands]
        if len(set(names)) != len(names):
            msg_6 = "sandbox command names must be unique"
            raise ValueError(msg_6)


class ContainerSandbox:
    """Hardened container sandbox for untrusted repository/build execution.

    Implements ``SandboxPort``. File reads/writes stay on the host through the
    composed ``ConstrainedLocalSandbox`` (identical traversal/symlink defenses);
    commands execute inside hardened containers confined to the same workspace.
    """

    _CAPABILITIES = SandboxCapabilities(
        file_api_confined=True,
        symlink_protected=True,
        environment_filtered=True,
        process_timeout=True,
        resource_limits=True,
        output_limited=True,
        process_filesystem_isolated=True,
        network_isolated=True,
        kernel_isolated=False,
    )

    def __init__(self, root: str | Path, *, config: ContainerSandboxConfig) -> None:
        self._files = ConstrainedLocalSandbox(
            root,
            commands=[],
            environment=config.environment,
            limits=config.limits,
        )
        self._root = self._files.root
        if "," in str(self._root):
            msg_11 = "sandbox root path must not contain ',' (breaks --mount bind parsing)"
            raise SandboxPathError(msg_11)
        self._image = config.image
        self._commands = MappingProxyType({item.name: item for item in config.commands})
        self._environment = MappingProxyType(dict(config.environment or {}))
        self._limits = config.limits
        self._network = config.network
        self._pids_limit = config.pids_limit
        self._tmpfs_bytes = config.tmpfs_bytes
        self._user = config.user
        self._docker = config.docker_executable
        self._active_container: str | None = None

    @property
    def capabilities(self) -> SandboxCapabilities:
        # Every supported network policy disables networking, so the isolation flags
        # are a truthful constant for this adapter's enforceable surface.
        return self._CAPABILITIES

    def read_text(self, relative_path: str) -> str:
        return self._files.read_text(relative_path)

    def write_text(self, relative_path: str, content: str) -> None:
        self._files.write_text(relative_path, content)

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        try:
            spec = self._commands[command_name]
        except KeyError as exc:
            msg_7 = f"command is not allowlisted: {command_name}"
            raise SandboxPolicyError(msg_7) from exc
        effective_timeout = spec.timeout_seconds
        if timeout_seconds is not None:
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                msg_8 = "runtime timeout must be positive and finite"
                raise SandboxPolicyError(msg_8)
            effective_timeout = min(effective_timeout, timeout_seconds)

        container_name = f"{_CONTAINER_NAME_PREFIX}{uuid.uuid4().hex[:12]}"
        argv = self._docker_run_argv(spec=spec, container_name=container_name)
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
                msg_12 = f"container runtime failed to start: {exc}"
                raise SandboxError(msg_12) from exc
            self._active_container = container_name
            try:
                try:
                    process.wait(timeout=effective_timeout)
                except subprocess.TimeoutExpired as exc:
                    self._kill_container(container_name, process=process)
                    try:
                        process.wait(timeout=_KILL_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        # The container is already killed; signaling the docker CLI's
                        # process group is best-effort cleanup of the client only.
                        with suppress(OSError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    msg_9 = f"command {command_name!r} exceeded {effective_timeout}s"
                    raise SandboxTimeoutError(msg_9) from exc
            finally:
                self._active_container = None

            stdout = self._read_output(stdout_file)
            stderr = self._read_output(stderr_file)
            if process.returncode == _DOCKER_CLI_ERROR_EXIT and stderr.startswith(
                _DOCKER_CLI_ERROR_MARKER
            ):
                msg_10 = f"container runtime failed: {stderr}"
                raise SandboxError(msg_10)
            return SandboxCommandResult(
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                succeeded=process.returncode in spec.allowed_exit_codes,
            )

    @property
    def active_container(self) -> str | None:
        """Name of the in-flight workload container, or None when idle."""
        return self._active_container

    def destroy(self) -> None:
        """Kill any in-flight workload container, leaving no running child workload."""
        active = self._active_container
        if active is not None:
            self._kill_container(active, process=None)
            self._active_container = None

    def __enter__(self) -> ContainerSandbox:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.destroy()

    def _docker_run_argv(self, *, spec: CommandSpec, container_name: str) -> tuple[str, ...]:
        argv = [
            self._docker,
            "run",
            "--rm",
            "--init",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            self._network.value,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--log-driver",
            "none",
            "--pids-limit",
            str(self._pids_limit),
            "--memory",
            str(self._limits.max_memory_bytes),
            "--memory-swap",
            str(self._limits.max_memory_bytes),
            "--ulimit",
            f"nofile={self._limits.max_open_files}:{self._limits.max_open_files}",
            "--ulimit",
            f"cpu={spec.cpu_seconds}:{spec.cpu_seconds}",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={self._tmpfs_bytes}",
            "--workdir",
            _WORKSPACE_MOUNT,
            "--mount",
            f"type=bind,source={self._root},target={_WORKSPACE_MOUNT}",
        ]
        if self._user is not None:
            argv.extend(["--user", self._user])
        for key, value in sorted(self._environment.items()):
            argv.extend(["--env", f"{key}={value}"])
        argv.append(self._image)
        argv.extend(spec.argv)
        return tuple(argv)

    def _kill_container(
        self, container_name: str, *, process: subprocess.Popen[bytes] | None
    ) -> None:
        # SIGKILL to the container destroys its PID namespace: no child workload of
        # the container survives, which subprocess-group kills cannot guarantee.
        # The kill is retried until it succeeds, the CLI exits, or the bounded deadline
        # passes: a timeout can fire while the container is still starting, in which
        # case a single-shot kill would miss and the workload would run unsupervised.
        deadline = time.monotonic() + _KILL_TIMEOUT_SECONDS
        while True:
            with suppress(OSError, subprocess.TimeoutExpired):
                completed = subprocess.run(
                    [self._docker, "kill", container_name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=max(deadline - time.monotonic(), _KILL_POLL_INTERVAL_SECONDS),
                    check=False,
                )
                if completed.returncode == 0:
                    return
            if process is not None and process.poll() is not None:
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(_KILL_POLL_INTERVAL_SECONDS)

    def _read_output(self, handle: BinaryIO) -> str:
        handle.seek(0)
        data = handle.read(self._limits.max_output_bytes + 1)
        if len(data) > self._limits.max_output_bytes:
            data = data[: self._limits.max_output_bytes]
        return data.decode("utf-8", errors="replace")
