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

import json
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
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

from loopforge.adapters.local_sandbox import (
    CommandSpec,
    ConstrainedLocalSandbox,
    SandboxLimits,
)
from loopforge.adapters.process_output import capture_output, close_pipes
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
class CommandOracle:
    """A frozen verifier in a separate container, with only bounded result data.

    argv receives one final JSON argument containing the producer's exit_code,
    stdout, stderr and output_truncated. It never mounts the producer workspace.
    A check without a producer runs only the verifier, over an empty observation.
    """

    command_name: str
    argv: tuple[str, ...]
    producer: bool = True
    max_observation_bytes: int = 64000

    def __post_init__(self) -> None:
        if (
            not self.command_name
            or not self.argv
            or not self.argv[0]
            or type(self.producer) is not bool
            or type(self.max_observation_bytes) is not int
            or not 1 <= self.max_observation_bytes <= 64000
        ):
            msg = "Invalid isolated command oracle"
            raise ValueError(msg)
        CommandSpec(name=self.command_name, argv=self.argv, timeout_seconds=1)


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
    workspace_read_only: bool = False
    # Code-owned extra read-only inputs, as (absolute host directory, absolute container
    # path): for example a harness-written script kept outside the workspace. Never the
    # workspace or /tmp; mounted read-only, so they grant no write path.
    read_only_mounts: tuple[tuple[str, str], ...] = ()
    oracles: tuple[CommandOracle, ...] = ()

    def __post_init__(self) -> None:
        if not self.image.strip() or any(char.isspace() for char in self.image):
            msg = "container image must be a non-empty reference without whitespace"
            raise ValueError(msg)
        if self.image.startswith("-"):
            # A leading-dash reference would be parsed as a `docker run` flag,
            # demoting the code-owned command argv to the image positional.
            msg_7 = "container image must not start with '-'"
            raise ValueError(msg_7)
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
        oracle_names = [oracle.command_name for oracle in self.oracles]
        if len(set(oracle_names)) != len(oracle_names) or not set(oracle_names) <= set(names):
            msg = "Oracle names must be unique registered commands"
            raise ValueError(msg)
        targets = [target for _source, target in self.read_only_mounts]
        for source, target in self.read_only_mounts:
            if (
                not Path(source).is_absolute()
                or not target.startswith("/")
                or ".." in Path(source).parts
                or ".." in target.split("/")
                or any(char in source + target for char in ",\n\0")
                or target.rstrip("/") in {_WORKSPACE_MOUNT, "/tmp", ""}
                or target.startswith((_WORKSPACE_MOUNT + "/", "/tmp/"))
            ):
                msg_8 = f"invalid read-only mount: {source!r} -> {target!r}"
                raise ValueError(msg_8)
        if len(set(targets)) != len(targets):
            msg_9 = "read-only mount targets must be unique"
            raise ValueError(msg_9)


def hardened_run_argv(
    config: ContainerSandboxConfig,
    root: Path,
    *,
    container_name: str,
    cpu_seconds: int,
    detached: bool = False,
) -> tuple[str, ...]:
    """The enforced ``docker run`` contract up to and including the image.

    One builder for every container this package starts, so a one-shot command and a
    persistent session can never drift apart in isolation flags.
    """
    argv = [
        config.docker_executable,
        "run",
        *(("--detach",) if detached else ()),
        "--rm",
        "--init",
        "--pull",
        "never",
        "--name",
        container_name,
        "--network",
        config.network.value,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--log-driver",
        "none",
        "--pids-limit",
        str(config.pids_limit),
        "--memory",
        str(config.limits.max_memory_bytes),
        "--memory-swap",
        str(config.limits.max_memory_bytes),
        "--ulimit",
        f"nofile={config.limits.max_open_files}:{config.limits.max_open_files}",
        "--ulimit",
        f"cpu={cpu_seconds}:{cpu_seconds}",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,size={config.tmpfs_bytes}",
        "--workdir",
        _WORKSPACE_MOUNT,
        "--mount",
        f"type=bind,source={root},target={_WORKSPACE_MOUNT}"
        + (",readonly" if config.workspace_read_only else ""),
    ]
    for source, target in config.read_only_mounts:
        argv.extend(["--mount", f"type=bind,source={source},target={target},readonly"])
    if config.user is not None:
        argv.extend(["--user", config.user])
    for key, value in sorted((config.environment or {}).items()):
        argv.extend(["--env", f"{key}={value}"])
    argv.append(config.image)
    return tuple(argv)


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
        self._workspace_read_only = config.workspace_read_only
        self._read_only_mounts = config.read_only_mounts
        self._config = config
        self._active_container: str | None = None
        self._oracle: ContainerSandbox | None = None

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

        oracle = next(
            (item for item in self._config.oracles if item.command_name == command_name), None
        )
        if oracle is not None:
            return self._run_oracle(spec, oracle, effective_timeout)
        return self._run_command(spec, effective_timeout)

    def _run_oracle(
        self, spec: CommandSpec, oracle: CommandOracle, timeout: float
    ) -> SandboxCommandResult:
        deadline = time.monotonic() + timeout
        observed = self._run_command(spec, timeout) if oracle.producer else None
        if observed is not None and observed.output_truncated:
            msg = "Truncated producer output cannot support an isolated oracle decision"
            raise SandboxPolicyError(msg)
        payload = json.dumps(
            {
                "exit_code": observed.exit_code if observed else None,
                "stdout": observed.stdout if observed else "",
                "stderr": observed.stderr if observed else "",
                "output_truncated": observed.output_truncated if observed else False,
            },
            ensure_ascii=False,
        )
        if len(payload.encode()) > oracle.max_observation_bytes:
            msg = "Producer observation exceeds the isolated oracle input allowance"
            raise SandboxPolicyError(msg)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = "Command deadline exhausted before isolated verification"
            raise SandboxTimeoutError(msg)
        command = replace(spec, argv=(*oracle.argv, payload))
        # Separate filesystem and PID namespaces; no candidate or extra inputs are
        # mounted in the verifier. Candidate text is data, never imported code.
        with tempfile.TemporaryDirectory(prefix="loopforge-oracle-") as root:
            config = replace(
                self._config,
                commands=(command,),
                oracles=(),
                read_only_mounts=(),
                workspace_read_only=True,
            )
            with type(self)(root, config=config) as verifier:
                self._oracle = verifier
                result = verifier.run(spec.name, timeout_seconds=remaining)
            # Keep the verifier reachable if execution or context cleanup failed,
            # so destroy() can retry any unconfirmed container cleanup.
            self._oracle = None
            return result

    def _run_command(self, spec: CommandSpec, effective_timeout: float) -> SandboxCommandResult:
        command_name = spec.name
        container_name = f"{_CONTAINER_NAME_PREFIX}{uuid.uuid4().hex[:12]}"
        argv = self._docker_run_argv(spec=spec, container_name=container_name)
        process = None
        cleanup_complete = False
        try:
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
            self._active_container = container_name
            captured = capture_output(process, effective_timeout, self._limits.max_output_bytes)
            if process.returncode == _DOCKER_CLI_ERROR_EXIT and captured.stderr_prefix.startswith(
                _DOCKER_CLI_ERROR_MARKER
            ):
                msg = f"container runtime failed: {captured.stderr}"
                raise SandboxError(msg)  # noqa: TRY301 - cleanup owns all failures
            cleanup_complete = True  # docker run returned after its container exited
            return SandboxCommandResult(
                exit_code=process.returncode,
                stdout=captured.stdout,
                stderr=captured.stderr,
                succeeded=process.returncode in spec.allowed_exit_codes,
                output_truncated=captured.truncated,
            )
        except BaseException as exc:
            # Retain the identity through cleanup, including interruption during wait.
            if process is None and not isinstance(exc, SandboxError):
                self._active_container = container_name
                cleanup_complete = self._kill_container(container_name, process=None)
            if process is not None:
                cleanup_complete = self._kill_container(container_name, process=process)
                try:
                    process.wait(timeout=_KILL_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    with suppress(OSError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=_KILL_TIMEOUT_SECONDS)
            if isinstance(exc, subprocess.TimeoutExpired):
                msg = f"command {command_name!r} exceeded {effective_timeout}s"
                raise SandboxTimeoutError(msg) from exc
            if isinstance(exc, OSError):
                msg = f"container output capture failed: {str(exc)[:300]}"
                raise SandboxError(msg) from exc
            raise
        finally:
            if process is not None:
                close_pipes(process)
            if cleanup_complete:
                self._active_container = None

    @property
    def active_container(self) -> str | None:
        """Name of the in-flight workload container, or None when idle."""
        return self._active_container

    def destroy(self) -> None:
        """Kill any in-flight workload container, leaving no running child workload."""
        if self._oracle is not None:
            self._oracle.destroy()
        active = self._active_container
        if active is not None:
            if self._kill_container(active, process=None):
                self._active_container = None
            else:
                msg = f"Container cleanup is unconfirmed: {active}"
                raise SandboxError(msg)

    def __enter__(self) -> ContainerSandbox:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.destroy()

    def _docker_run_argv(self, *, spec: CommandSpec, container_name: str) -> tuple[str, ...]:
        return (
            *hardened_run_argv(
                self._config,
                self._root,
                container_name=container_name,
                cpu_seconds=spec.cpu_seconds,
            ),
            *spec.argv,
        )

    def _kill_container(
        self, container_name: str, *, process: subprocess.Popen[bytes] | None
    ) -> bool:
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
                    return True
            if process is not None and process.poll() is not None:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(_KILL_POLL_INTERVAL_SECONDS)
