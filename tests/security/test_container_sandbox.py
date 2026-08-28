from __future__ import annotations

import math
import subprocess
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, ClassVar

import pytest

from loopforge.adapters.container_sandbox import (
    ContainerNetworkPolicy,
    ContainerSandbox,
    ContainerSandboxConfig,
)
from loopforge.adapters.local_sandbox import (
    CommandSpec,
    ConstrainedLocalSandbox,
    SandboxLimits,
)
from loopforge.adapters.sandbox_tools import SandboxCommandTools, SandboxToolBinding
from loopforge.adapters.scripted import FixedClock
from loopforge.adapters.telemetry import NoOpTelemetry, TelemetrySandbox
from loopforge.domain.security import SandboxCapabilities, SandboxRequirements
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import Permission, RiskLevel
from loopforge.ports.sandbox import (
    SandboxCommandResult,
    SandboxError,
    SandboxPathError,
    SandboxPolicyError,
    SandboxTimeoutError,
)

_TEST_IMAGE = "alpine:3.21"


def _image_available() -> bool:
    # Docker Desktop's containerd image store can fail short-name resolution in
    # `image inspect` ("No such image") while `docker run` works; the canonical
    # fully-qualified reference is the reliable probe.
    for reference in (_TEST_IMAGE, f"docker.io/library/{_TEST_IMAGE}"):
        try:
            image = subprocess.run(
                ["docker", "image", "inspect", reference],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if image.returncode == 0:
            return True
    return False


def _docker_ready() -> bool:
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and _image_available()


_REQUIRES_DOCKER = pytest.mark.skipif(
    not _docker_ready(),
    reason=(
        f"docker daemon or {_TEST_IMAGE} test image unavailable; start Docker and run "
        f"`docker pull {_TEST_IMAGE}` to execute live container isolation tests"
    ),
)


def _sh(script: str, *, name: str = "sh", timeout_seconds: float = 30.0) -> CommandSpec:
    return CommandSpec(
        name=name,
        argv=("/bin/sh", "-c", script),
        timeout_seconds=timeout_seconds,
        cpu_seconds=20,
    )


_BASE_CONFIG = ContainerSandboxConfig(image=_TEST_IMAGE, commands=(_sh("true"),))


def _config(**changes: object) -> ContainerSandboxConfig:
    return replace(_BASE_CONFIG, **changes)


def _sandbox(root: Path, config: ContainerSandboxConfig = _BASE_CONFIG) -> ContainerSandbox:
    return ContainerSandbox(root, config=config)


def _metadata(name: str = "inspect") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _binding(
    name: str = "inspect",
    *,
    requirements: SandboxRequirements | None = None,
) -> SandboxToolBinding:
    return SandboxToolBinding(
        metadata=_metadata(name),
        command_name="sh",
        requirements=requirements or SandboxRequirements(),
    )


def _sandbox_container_names() -> list[str]:
    completed = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=loopforge-sandbox-",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        check=False,
        timeout=15,
        text=True,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


# --- Constructor validation ---


def test_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises(SandboxPathError, match="sandbox root must exist"):
        _sandbox(tmp_path / "missing")


def test_root_must_be_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(SandboxPathError, match="sandbox root must be an existing directory"):
        _sandbox(target)


@pytest.mark.parametrize("image", ["", "   ", "alpine:3.21 rm -rf"])
def test_image_must_be_a_clean_reference(tmp_path: Path, image: str) -> None:
    with pytest.raises(ValueError, match="container image must be a non-empty reference"):
        _sandbox(tmp_path, _config(image=image))


@pytest.mark.parametrize("image", ["--privileged", "--network=host", "-v/host:/host"])
def test_image_must_never_become_a_docker_flag(tmp_path: Path, image: str) -> None:
    # A leading-dash reference would be parsed as a `docker run` flag,
    # demoting the code-owned command argv to the image positional.
    with pytest.raises(ValueError, match="must not start with '-'"):
        _config(image=image)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"pids_limit": 0}, "pids/tmpfs limits must be positive"),
        ({"tmpfs_bytes": -1}, "pids/tmpfs limits must be positive"),
        ({"user": " "}, "container user must be non-empty"),
        ({"user": "root root"}, "container user must be non-empty"),
        ({"docker_executable": ""}, "docker executable must be non-empty"),
    ],
)
def test_container_options_are_validated(
    tmp_path: Path, kwargs: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        _config(**kwargs)


@pytest.mark.parametrize("key", ["", "1BAD", "HAS-DASH", "INJECT=ED"])
def test_environment_keys_must_be_safe_names(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="invalid container environment variable name"):
        _config(environment={key: "value"})


def test_duplicate_command_names_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sandbox command names must be unique"):
        _config(commands=(_sh("true"), _sh("false")))


def test_default_network_policy_is_deny_all(tmp_path: Path) -> None:
    assert (
        _sandbox(tmp_path)._network  # pyright: ignore[reportPrivateUsage]  # white-box policy pin
        is ContainerNetworkPolicy.NONE
    )


# --- Capability report and fail-closed negotiation ---


def test_capability_report_is_honest_container_profile(tmp_path: Path) -> None:
    assert _sandbox(tmp_path).capabilities == SandboxCapabilities(
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


def test_network_requiring_workload_binds_to_container_but_not_local(tmp_path: Path) -> None:
    requirements = SandboxRequirements(network_isolated=True, process_filesystem_isolated=True)
    binding = _binding(requirements=requirements)
    container_tools = SandboxCommandTools(_sandbox(tmp_path), [binding])
    assert container_tools.metadata_for("inspect").name == "inspect"
    local = ConstrainedLocalSandbox(tmp_path, commands=[])
    with pytest.raises(ValueError, match="sandbox cannot satisfy tool 'inspect'") as excinfo:
        SandboxCommandTools(local, [binding])
    assert "network_isolated" in str(excinfo.value)
    assert "process_filesystem_isolated" in str(excinfo.value)


def test_kernel_isolation_requirement_fails_closed_against_container(tmp_path: Path) -> None:
    binding = _binding(requirements=SandboxRequirements(kernel_isolated=True))
    with pytest.raises(ValueError, match="kernel_isolated"):
        SandboxCommandTools(_sandbox(tmp_path), [binding])


def test_telemetry_wrapper_preserves_container_capabilities(tmp_path: Path) -> None:
    wrapped = TelemetrySandbox(
        _sandbox(tmp_path),
        NoOpTelemetry(),
        clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    assert wrapped.capabilities == _sandbox(tmp_path).capabilities
    assert wrapped.capabilities.network_isolated is True


# --- File API: local adapter defenses retained via composition ---


def test_file_api_round_trip(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_text("pkg/out/result.txt", "héllo")
    assert sandbox.read_text("pkg/out/result.txt") == "héllo"


@pytest.mark.parametrize("path", ["../escape.txt", "a/../../escape.txt", "/etc/passwd"])
def test_file_api_rejects_traversal_and_absolute_paths(tmp_path: Path, path: str) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPathError):
        sandbox.read_text(path)
    with pytest.raises(SandboxPathError):
        sandbox.write_text(path, "pwned")
    assert list(tmp_path.rglob("*")) == []


def test_file_api_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("host-secret", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "link.txt").symlink_to(secret)
    sandbox = _sandbox(root)
    with pytest.raises(SandboxPathError):
        sandbox.read_text("link.txt")
    with pytest.raises(SandboxPathError):
        sandbox.write_text("link.txt", "overwrite")
    assert secret.read_text(encoding="utf-8") == "host-secret"


def test_file_api_enforces_byte_limits(tmp_path: Path) -> None:
    limits = SandboxLimits(max_read_bytes=4, max_write_bytes=4)
    sandbox = _sandbox(tmp_path, _config(limits=limits))
    with pytest.raises(SandboxPolicyError, match="write exceeds sandbox byte limit"):
        sandbox.write_text("big.txt", "ééé")
    sandbox.write_text("ok.txt", "abcd")
    (tmp_path / "big.txt").write_text("12345", encoding="utf-8")
    with pytest.raises(SandboxPolicyError, match="read exceeds sandbox byte limit"):
        sandbox.read_text("big.txt")


# --- docker run argv shape (deterministic enforcement surface) ---


def test_docker_run_argv_enforces_hardened_contract(tmp_path: Path) -> None:
    spec = CommandSpec(
        name="build",
        argv=("/usr/bin/make", "all"),
        timeout_seconds=30.0,
        cpu_seconds=7,
    )
    sandbox = _sandbox(
        tmp_path,
        _config(
            commands=(spec,),
            environment={"B_FLAG": "2", "A_FLAG": "1"},
            limits=SandboxLimits(max_memory_bytes=64 * 1024 * 1024, max_open_files=64),
            pids_limit=32,
            tmpfs_bytes=8 * 1024 * 1024,
            user="1000:1000",
        ),
    )
    argv = sandbox._docker_run_argv(  # pyright: ignore[reportPrivateUsage]  # enforcement-surface pin
        spec=spec, container_name="test-name"
    )
    assert argv == (
        "docker",
        "run",
        "--rm",
        "--init",
        "--pull",
        "never",
        "--name",
        "test-name",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--log-driver",
        "none",
        "--pids-limit",
        "32",
        "--memory",
        str(64 * 1024 * 1024),
        "--memory-swap",
        str(64 * 1024 * 1024),
        "--ulimit",
        "nofile=64:64",
        "--ulimit",
        "cpu=7:7",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,size={8 * 1024 * 1024}",
        "--workdir",
        "/workspace",
        "--mount",
        f"type=bind,source={tmp_path},target=/workspace",
        "--user",
        "1000:1000",
        "--env",
        "A_FLAG=1",
        "--env",
        "B_FLAG=2",
        _TEST_IMAGE,
        "/usr/bin/make",
        "all",
    )


def test_docker_run_argv_omits_user_when_unset(tmp_path: Path) -> None:
    spec = _sh("true")
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    argv = sandbox._docker_run_argv(  # pyright: ignore[reportPrivateUsage]  # enforcement-surface pin
        spec=spec, container_name="test-name"
    )
    assert "--user" not in argv
    assert argv[-4:] == (_TEST_IMAGE, "/bin/sh", "-c", "true")


# --- Plumbing with a faked container runtime (no daemon required) ---


class _FakePopen:
    """Minimal subprocess.Popen stub with class-level scripted behavior."""

    instances: ClassVar[list[_FakePopen]] = []
    wait_behavior: ClassVar[str] = "exit"
    exit_code: ClassVar[int] = 0
    out: ClassVar[bytes] = b""
    err: ClassVar[bytes] = b""

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
        **_kwargs: object,
    ) -> None:
        self.argv = argv
        self.pid = -1  # never signalable; no real process group exists
        self.returncode: int | None = None
        self._wait_calls = 0
        self._release = threading.Event()
        stdout.write(type(self).out)
        stderr.write(type(self).err)
        type(self).instances.append(self)

    def wait(self, timeout: float | None = None) -> int:
        behavior = type(self).wait_behavior
        if behavior in ("timeout", "timeout-twice"):
            self._wait_calls += 1
            if self._wait_calls == 1 or (behavior == "timeout-twice" and self._wait_calls == 2):
                raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout or 0.0)
            self.returncode = 137
            return self.returncode
        if behavior == "timeout-exited":
            # CLI dies concurrently with the timeout: wait raises, but poll() already
            # reports an exit code.
            self.returncode = 137
            self._wait_calls += 1
            if self._wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout or 0.0)
            return self.returncode
        if behavior == "block":
            if not self._release.wait(timeout=timeout) or self.returncode is None:
                raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout or 0.0)
            return self.returncode
        self.returncode = type(self).exit_code
        return self.returncode

    def release(self, exit_code: int) -> None:
        self.returncode = exit_code
        self._release.set()

    def poll(self) -> int | None:
        return self.returncode


_SCRIPTED_KILL_CODES: list[int] = []
_KILL_DEFAULT_CODE: list[int] = [0]


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    _FakePopen.instances = []
    _FakePopen.wait_behavior = "exit"
    _FakePopen.exit_code = 0
    _FakePopen.out = b""
    _FakePopen.err = b""
    kill_calls: list[tuple[str, ...]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[list[str]]:
        kill_calls.append(tuple(argv))
        code = _SCRIPTED_KILL_CODES.pop(0) if _SCRIPTED_KILL_CODES else _KILL_DEFAULT_CODE[0]
        return subprocess.CompletedProcess(argv, code)

    _SCRIPTED_KILL_CODES.clear()
    _KILL_DEFAULT_CODE[0] = 0
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return kill_calls


def test_unknown_command_fails_before_spawning(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    with pytest.raises(SandboxPolicyError, match="command is not allowlisted"):
        _sandbox(tmp_path).run("nope")
    assert _FakePopen.instances == []


def test_runtime_timeout_must_be_positive(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    with pytest.raises(SandboxPolicyError, match="runtime timeout must be positive"):
        _sandbox(tmp_path).run("sh", timeout_seconds=0)
    assert _FakePopen.instances == []


def test_success_result_maps_exit_code_and_output(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    result = _sandbox(tmp_path).run("sh")
    assert result.exit_code == 0
    assert result.succeeded is True
    assert isinstance(result, SandboxCommandResult)
    assert _FakePopen.instances[0].argv[1] == "run"


def test_timeout_kills_named_container_and_clears_active(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    _FakePopen.wait_behavior = "timeout"
    spec = _sh("sleep 60", timeout_seconds=60.0)
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    with pytest.raises(SandboxTimeoutError, match=r"exceeded 1\.0s"):
        sandbox.run("sh", timeout_seconds=1.0)
    assert len(fake_runtime) == 1
    kill_argv = fake_runtime[0]
    assert kill_argv[:2] == ("docker", "kill")
    assert kill_argv[2].startswith("loopforge-sandbox-")
    assert sandbox.active_container is None


def test_stubborn_cli_process_group_is_killed_after_container_kill(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    _FakePopen.wait_behavior = "timeout-twice"
    spec = _sh("sleep 60", timeout_seconds=60.0)
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    with pytest.raises(SandboxTimeoutError, match=r"exceeded 1\.0s"):
        sandbox.run("sh", timeout_seconds=1.0)
    assert len(fake_runtime) == 1
    assert fake_runtime[0][:2] == ("docker", "kill")
    assert sandbox.active_container is None


def test_container_kill_is_retried_until_it_succeeds(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    # A timeout can fire while the container is still starting; a single-shot kill
    # would miss ("No such container") and leave the workload running unsupervised.
    _FakePopen.wait_behavior = "timeout"
    _SCRIPTED_KILL_CODES.extend([1, 1])
    spec = _sh("sleep 60", timeout_seconds=60.0)
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    with pytest.raises(SandboxTimeoutError):
        sandbox.run("sh", timeout_seconds=1.0)
    assert len(fake_runtime) == 3


def test_container_kill_stops_retrying_once_cli_has_exited(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    _FakePopen.wait_behavior = "timeout-exited"
    _SCRIPTED_KILL_CODES.extend([1, 1, 1, 1, 1])
    spec = _sh("sleep 60", timeout_seconds=60.0)
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    with pytest.raises(SandboxTimeoutError):
        sandbox.run("sh", timeout_seconds=1.0)
    assert len(fake_runtime) == 1


def test_destroy_retries_kill_until_container_dies(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    _FakePopen.wait_behavior = "block"
    _SCRIPTED_KILL_CODES.extend([1])
    spec = _sh("sleep 60", timeout_seconds=120.0)
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    outcome: list[object] = []
    worker = threading.Thread(target=lambda: outcome.append(sandbox.run("sh")))
    worker.start()
    deadline = time.monotonic() + 5
    while sandbox.active_container is None and time.monotonic() < deadline:
        time.sleep(0.01)
    sandbox.destroy()
    _FakePopen.instances[0].release(137)
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(fake_runtime) == 2


def test_kill_gives_up_after_bounded_deadline(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A kill that never succeeds must not loop forever: the retry loop is bounded.
    _KILL_DEFAULT_CODE[0] = 1
    monkeypatch.setattr("loopforge.adapters.container_sandbox._KILL_TIMEOUT_SECONDS", 0.3)
    _FakePopen.wait_behavior = "timeout"
    spec = _sh("sleep 60", timeout_seconds=60.0)
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    started = time.monotonic()
    with pytest.raises(SandboxTimeoutError):
        sandbox.run("sh", timeout_seconds=1.0)
    assert time.monotonic() - started < 30
    assert fake_runtime, "at least one kill attempt must be recorded"


def test_captured_output_is_truncated_to_the_configured_bound(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    _FakePopen.out = b"0123456789"
    limits = SandboxLimits(max_output_bytes=4)
    result = _sandbox(tmp_path, _config(limits=limits)).run("sh")
    assert result.stdout == "0123"


def test_missing_docker_executable_surfaces_sandbox_error(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, _config(docker_executable="/definitely/missing/docker"))
    with pytest.raises(SandboxError, match="container runtime failed to start"):
        sandbox.run("sh")


def test_symlinked_root_is_resolved_before_mounting(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    spec = _sh("true")
    sandbox = _sandbox(link, _config(commands=(spec,)))
    argv = sandbox._docker_run_argv(  # pyright: ignore[reportPrivateUsage]  # enforcement-surface pin
        spec=spec, container_name="test-name"
    )
    mount = argv[argv.index("--mount") + 1]
    assert mount == f"type=bind,source={real.resolve()},target=/workspace"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_runtime_timeout_must_be_finite(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]], bad: float
) -> None:
    with pytest.raises(SandboxPolicyError, match="runtime timeout must be positive and finite"):
        _sandbox(tmp_path).run("sh", timeout_seconds=bad)
    assert _FakePopen.instances == []


def test_docker_cli_failure_surfaces_as_sandbox_error(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    _FakePopen.exit_code = 125
    _FakePopen.err = b"docker: Error response from daemon: no such image.\n"
    with pytest.raises(SandboxError, match="container runtime failed"):
        _sandbox(tmp_path).run("sh")


def test_destroy_kills_in_flight_container(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    _FakePopen.wait_behavior = "block"
    spec = _sh("sleep 60", timeout_seconds=120.0)
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    outcome: list[object] = []

    def target() -> None:
        outcome.append(sandbox.run("sh"))

    worker = threading.Thread(target=target)
    worker.start()
    deadline = time.monotonic() + 5
    while sandbox.active_container is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sandbox.active_container is not None
    sandbox.destroy()
    _FakePopen.instances[0].release(137)
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert fake_runtime, "destroy must kill the active container"
    assert sandbox.active_container is None
    result = outcome[0]
    assert isinstance(result, SandboxCommandResult)
    assert result.succeeded is False


def test_destroy_without_active_container_is_a_noop(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    _sandbox(tmp_path).destroy()
    assert fake_runtime == []


def test_context_manager_destroys_on_exit(
    tmp_path: Path, fake_runtime: list[tuple[str, ...]]
) -> None:
    sandbox = _sandbox(tmp_path)
    with sandbox:
        sandbox._active_container = "loopforge-sandbox-faked"  # pyright: ignore[reportPrivateUsage]  # simulated in-flight workload
    assert fake_runtime == [("docker", "kill", "loopforge-sandbox-faked")]


# --- Live container isolation evidence (capability-gated) ---


@_REQUIRES_DOCKER
def test_live_workload_cannot_see_host_filesystem(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "host-secret.txt"
    secret.write_text("host-secret-value", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    probe = _sh(
        f"cat {secret} >/dev/null 2>&1 && echo host-path-readable || echo host-path-unreadable; "
        "grep -c ' / ' /proc/mounts >/dev/null && echo mounts-probed"
    )
    result = _sandbox(root, _config(commands=(probe,))).run("sh")
    assert "host-secret-value" not in result.stdout
    assert "host-path-readable" not in result.stdout
    assert "host-path-unreadable" in result.stdout
    assert "mounts-probed" in result.stdout


@_REQUIRES_DOCKER
def test_live_workload_cannot_write_outside_workspace(tmp_path: Path) -> None:
    probe = _sh(
        "touch /etc/pwned 2>&1 || echo rootfs-write-blocked; "
        "touch /workspace/ok.txt && echo workspace-write-ok"
    )
    result = _sandbox(tmp_path, _config(commands=(probe,))).run("sh")
    assert "rootfs-write-blocked" in result.stdout
    assert "workspace-write-ok" in result.stdout
    assert (tmp_path / "ok.txt").is_file()


@_REQUIRES_DOCKER
def test_live_network_is_unreachable(tmp_path: Path) -> None:
    probe = _sh("wget -q -T 3 -O- http://example.com 2>&1; echo http-exit=$?")
    result = _sandbox(tmp_path, _config(commands=(probe,))).run("sh")
    assert "http-exit=0" not in result.stdout
    assert "Example Domain" not in result.stdout


@_REQUIRES_DOCKER
def test_live_host_environment_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOPFORGE_HOST_MARKER", "leaked-secret")
    probe = _sh("echo marker=${LOOPFORGE_HOST_MARKER:-unset}; echo explicit=${EXPLICIT:-unset}")
    sandbox = _sandbox(tmp_path, _config(commands=(probe,), environment={"EXPLICIT": "visible"}))
    result = sandbox.run("sh")
    assert "marker=unset" in result.stdout
    assert "explicit=visible" in result.stdout
    assert "leaked-secret" not in result.stdout


@_REQUIRES_DOCKER
def test_live_timeout_kills_container_and_children(tmp_path: Path) -> None:
    spec = _sh("sleep 300 & sleep 300 & wait", timeout_seconds=120.0)
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    started = time.monotonic()
    with pytest.raises(SandboxTimeoutError, match=r"exceeded 1\.0s"):
        sandbox.run("sh", timeout_seconds=1.0)
    elapsed = time.monotonic() - started
    assert elapsed < 60
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _sandbox_container_names():
        time.sleep(0.2)
    assert _sandbox_container_names() == [], "timed-out sandbox containers must be gone"


@_REQUIRES_DOCKER
def test_live_cpu_limit_kills_busy_workload(tmp_path: Path) -> None:
    spec = CommandSpec(
        name="burn",
        argv=("/bin/sh", "-c", "while :; do :; done"),
        timeout_seconds=60.0,
        cpu_seconds=2,
    )
    started = time.monotonic()
    result = _sandbox(tmp_path, _config(commands=(spec,))).run("burn")
    elapsed = time.monotonic() - started
    assert result.succeeded is False
    assert result.exit_code != 0
    assert elapsed < 60, "hard CPU limit must terminate the workload externally"


@_REQUIRES_DOCKER
def test_live_pids_limit_bounds_forking(tmp_path: Path) -> None:
    probe = _sh("for i in $(seq 1 100); do sleep 60 & done; wait", timeout_seconds=120.0)
    sandbox = _sandbox(tmp_path, _config(commands=(probe,), pids_limit=16))
    started = time.monotonic()
    result = sandbox.run("sh")
    elapsed = time.monotonic() - started
    assert result.succeeded is False
    assert "can't fork" in result.stderr.lower()
    assert elapsed < 60, "pids limit must abort the fork storm, not queue 100 children"


@_REQUIRES_DOCKER
def test_live_memory_limit_kills_hog(tmp_path: Path) -> None:
    probe = _sh("tail -c 100000000 /dev/zero > /dev/null", timeout_seconds=60.0)
    limits = SandboxLimits(max_memory_bytes=32 * 1024 * 1024)
    started = time.monotonic()
    result = _sandbox(tmp_path, _config(commands=(probe,), limits=limits)).run("sh")
    elapsed = time.monotonic() - started
    assert result.succeeded is False
    assert result.exit_code != 0
    assert elapsed < 60, "memory cgroup must kill the hog externally"


@_REQUIRES_DOCKER
def test_live_tmpfs_is_size_bounded(tmp_path: Path) -> None:
    probe = _sh("dd if=/dev/zero of=/tmp/big bs=1M count=64 2>&1; echo dd-exit=$?")
    sandbox = _sandbox(tmp_path, _config(commands=(probe,), tmpfs_bytes=8 * 1024 * 1024))
    result = sandbox.run("sh")
    assert "dd-exit=0" not in result.stdout
    assert "No space left" in result.stdout


@_REQUIRES_DOCKER
def test_live_output_is_bounded(tmp_path: Path) -> None:
    probe = _sh("yes AAAAAAAAAAAAAAAAAAAAAAAA | head -c 2000000", timeout_seconds=60.0)
    limits = SandboxLimits(max_output_bytes=4096)
    result = _sandbox(tmp_path, _config(commands=(probe,), limits=limits)).run("sh")
    assert len(result.stdout.encode("utf-8")) == 4096


@_REQUIRES_DOCKER
def test_live_allowed_exit_codes_are_respected(tmp_path: Path) -> None:
    spec = CommandSpec(
        name="exit3",
        argv=("/bin/sh", "-c", "exit 3"),
        timeout_seconds=30.0,
        cpu_seconds=10,
        allowed_exit_codes=frozenset({0, 3}),
    )
    result = _sandbox(tmp_path, _config(commands=(spec,))).run("exit3")
    assert result.exit_code == 3
    assert result.succeeded is True


@_REQUIRES_DOCKER
def test_live_missing_image_fails_as_sandbox_error(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, _config(image="loopforge-definitely-missing-image:never"))
    with pytest.raises(SandboxError, match="container runtime failed"):
        sandbox.run("sh")


@_REQUIRES_DOCKER
def test_live_destroy_leaves_no_running_workload(tmp_path: Path) -> None:
    spec = _sh("sleep 300", timeout_seconds=300.0)
    sandbox = _sandbox(tmp_path, _config(commands=(spec,)))
    outcome: list[object] = []
    worker = threading.Thread(target=lambda: outcome.append(sandbox.run("sh")))
    worker.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not _sandbox_container_names():
        time.sleep(0.1)
    assert _sandbox_container_names(), "workload container should be running"
    sandbox.destroy()
    worker.join(timeout=60)
    assert not worker.is_alive()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _sandbox_container_names():
        time.sleep(0.2)
    assert _sandbox_container_names() == [], "destroy must leave no running child workload"
    result = outcome[0]
    assert isinstance(result, SandboxCommandResult)
    assert result.succeeded is False
