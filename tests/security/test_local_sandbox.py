from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from loopforge.adapters.local_sandbox import (
    CommandSpec,
    ConstrainedLocalSandbox,
    SandboxLimits,
)
from loopforge.domain.security import SandboxRequirements
from loopforge.ports.sandbox import (
    SandboxError,
    SandboxPathError,
    SandboxPolicyError,
    SandboxTimeoutError,
)

EXIT_OK = frozenset({0})
_SAFE_COMPONENT = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,11}", fullmatch=True)


def _rlimit_as_supported() -> bool:
    probe = "import resource; resource.setrlimit(resource.RLIMIT_AS, (268435456, 268435456))"
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, check=False)
    return completed.returncode == 0


# Every successful `run()` spawns `_sandbox_exec.py`, which unconditionally applies
# RLIMIT_AS. On this macOS kernel the kernel rejects that call with EINVAL, so
# process-execution behavior is exercised only where the platform supports it.
_REQUIRES_RLIMIT_AS = pytest.mark.skipif(
    not _rlimit_as_supported(),
    reason="platform rejects setrlimit(RLIMIT_AS); launcher cannot apply resource limits",
)


def _command(
    name: str,
    argv: tuple[str, ...],
    *,
    timeout_seconds: float = 10.0,
    allowed_exit_codes: frozenset[int] = EXIT_OK,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        argv=argv,
        timeout_seconds=timeout_seconds,
        allowed_exit_codes=allowed_exit_codes,
    )


def _sh(script: str, *, name: str = "sh") -> CommandSpec:
    return _command(name, ("/bin/sh", "-c", script))


def _sandbox(
    root: Path,
    *,
    commands: list[CommandSpec] | None = None,
    environment: dict[str, str] | None = None,
    limits: SandboxLimits | None = None,
) -> ConstrainedLocalSandbox:
    return ConstrainedLocalSandbox(
        root,
        commands=commands if commands is not None else [_sh("/usr/bin/true")],
        environment=environment,
        limits=limits if limits is not None else SandboxLimits(),
    )


def test_sandbox_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="limits must be positive"):
        SandboxLimits(max_read_bytes=0)
    with pytest.raises(ValueError, match="limits must be positive"):
        SandboxLimits(max_open_files=-1)


def test_command_spec_requires_name_and_argv() -> None:
    with pytest.raises(ValueError, match="name and argv are required"):
        CommandSpec(name="  ", argv=("/usr/bin/true",), timeout_seconds=5.0)
    with pytest.raises(ValueError, match="name and argv are required"):
        CommandSpec(name="empty", argv=(), timeout_seconds=5.0)


def test_command_spec_requires_absolute_executable() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        CommandSpec(name="sh", argv=("sh", "-c", "true"), timeout_seconds=5.0)


def test_command_spec_requires_positive_time_limits() -> None:
    with pytest.raises(ValueError, match="time limits must be positive"):
        CommandSpec(name="t", argv=("/usr/bin/true",), timeout_seconds=0.0)
    with pytest.raises(ValueError, match="time limits must be positive"):
        CommandSpec(name="t", argv=("/usr/bin/true",), timeout_seconds=5.0, cpu_seconds=-1)


def test_command_spec_requires_non_empty_allowed_exit_codes() -> None:
    with pytest.raises(ValueError, match="allowed_exit_codes cannot be empty"):
        CommandSpec(
            name="t",
            argv=("/usr/bin/true",),
            timeout_seconds=5.0,
            allowed_exit_codes=frozenset(),
        )


def test_sandbox_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises(SandboxPathError, match="root must exist"):
        _sandbox(tmp_path / "missing")


def test_sandbox_root_must_be_a_directory(tmp_path: Path) -> None:
    file_root = tmp_path / "file.txt"
    file_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(SandboxPathError, match="existing directory"):
        _sandbox(file_root)


def test_sandbox_rejects_duplicate_command_names(tmp_path: Path) -> None:
    command = _command("dup", ("/usr/bin/true",))
    with pytest.raises(ValueError, match="must be unique"):
        ConstrainedLocalSandbox(tmp_path, commands=[command, command])


def test_write_then_read_round_trips_and_creates_parent_directories(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_text("nested/deep/note.txt", "héllo ✓")

    assert sandbox.read_text("nested/deep/note.txt") == "héllo ✓"
    assert not list(tmp_path.glob("**/.loopforge-*"))


@pytest.mark.parametrize("relative", ["..", "../escape.txt", "a/../../escape.txt"])
def test_read_text_rejects_parent_traversal(tmp_path: Path, relative: str) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPathError, match="cannot contain"):
        sandbox.read_text(relative)


@pytest.mark.parametrize("relative", ["..", "../escape.txt", "a/../../escape.txt"])
def test_write_text_rejects_parent_traversal(tmp_path: Path, relative: str) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPathError, match="cannot contain"):
        sandbox.write_text(relative, "payload")
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.parametrize("relative", ["/etc/hostname", "/etc/passwd"])
def test_read_text_rejects_absolute_paths(tmp_path: Path, relative: str) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPathError, match="must be relative"):
        sandbox.read_text(relative)


def test_write_text_rejects_absolute_paths(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPathError, match="must be relative"):
        sandbox.write_text(str(tmp_path / "absolute.txt"), "payload")


def test_read_text_rejects_missing_path(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPathError, match="does not exist"):
        sandbox.read_text("missing.txt")


def test_read_text_rejects_directory(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPathError, match="regular file"):
        sandbox.read_text("dir")


def test_read_text_enforces_byte_limit(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("1234", encoding="utf-8")
    (tmp_path / "big.txt").write_text("12345", encoding="utf-8")
    sandbox = _sandbox(tmp_path, limits=SandboxLimits(max_read_bytes=4))

    assert sandbox.read_text("ok.txt") == "1234"
    with pytest.raises(SandboxPolicyError, match="byte limit"):
        sandbox.read_text("big.txt")


def test_write_text_enforces_byte_limit(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, limits=SandboxLimits(max_write_bytes=4))

    sandbox.write_text("ok.txt", "1234")
    assert (tmp_path / "ok.txt").read_text(encoding="utf-8") == "1234"

    with pytest.raises(SandboxPolicyError, match="byte limit"):
        sandbox.write_text("big.txt", "12345")
    assert not (tmp_path / "big.txt").exists()

    # The limit is on UTF-8 bytes, not characters: "ééé" encodes to 6 bytes.
    with pytest.raises(SandboxPolicyError, match="byte limit"):
        sandbox.write_text("utf8.txt", "ééé")
    assert not (tmp_path / "utf8.txt").exists()


def test_read_text_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    sandbox = _sandbox(root)
    with pytest.raises(SandboxPathError, match="symlink"):
        sandbox.read_text("link.txt")


def test_write_text_rejects_symlinked_directory_component(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (root / "dirlink").symlink_to(outside_dir)

    sandbox = _sandbox(root)
    with pytest.raises(SandboxPathError, match="symlink"):
        sandbox.write_text("dirlink/new.txt", "payload")
    assert list(outside_dir.iterdir()) == []


def test_write_text_rejects_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    sandbox = _sandbox(root)
    with pytest.raises(SandboxPathError, match="symlink"):
        sandbox.write_text("link.txt", "overwritten")
    assert outside.read_text(encoding="utf-8") == "original"


def test_write_text_rejects_directory_target(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPathError, match="regular non-symlink"):
        sandbox.write_text("dir", "payload")


def test_write_failure_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_text("data.txt", "original")

    def fail_replace(self: Path, target: Path) -> Path:
        msg = "simulated replace failure"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(SandboxError, match="simulated replace failure"):
        sandbox.write_text("data.txt", "replacement that must not land")

    assert (tmp_path / "data.txt").read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".loopforge-*"))


def test_run_rejects_unknown_command(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPolicyError, match="not allowlisted"):
        sandbox.run("rm-everything")


@_REQUIRES_RLIMIT_AS
def test_run_maps_disallowed_exit_code_to_failure(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path,
        commands=[
            _command("ok", ("/usr/bin/true",)),
            _command("fail", ("/usr/bin/false",)),
        ],
    )

    success = sandbox.run("ok")
    assert success.exit_code == 0
    assert success.succeeded is True

    failure = sandbox.run("fail")
    assert failure.exit_code == 1
    assert failure.succeeded is False


@_REQUIRES_RLIMIT_AS
def test_run_honors_configured_allowed_exit_codes(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path,
        commands=[_command("fail-ok", ("/usr/bin/false",), allowed_exit_codes=frozenset({1}))],
    )
    result = sandbox.run("fail-ok")
    assert result.exit_code == 1
    assert result.succeeded is True


@_REQUIRES_RLIMIT_AS
def test_run_executes_argv_without_shell_interpretation(tmp_path: Path) -> None:
    marker_name = "literal;$(touch pwned).txt"
    (tmp_path / marker_name).write_text("payload", encoding="utf-8")
    sandbox = _sandbox(tmp_path, commands=[_command("cat", ("/bin/cat", marker_name))])

    result = sandbox.run("cat")

    assert result.succeeded is True
    assert result.stdout == "payload"
    # If a shell had interpreted the argument, `touch pwned` would have run.
    assert not (tmp_path / "pwned").exists()


@_REQUIRES_RLIMIT_AS
def test_run_executes_in_sandbox_root(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("from-root", encoding="utf-8")
    sandbox = _sandbox(tmp_path, commands=[_sh("/bin/cat data.txt")])

    result = sandbox.run("sh", timeout_seconds=5.0)

    assert result.succeeded is True
    assert result.stdout == "from-root"


@_REQUIRES_RLIMIT_AS
def test_run_filters_environment_and_does_not_inherit_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOPFORGE_HOST_MARKER", "host-secret")
    sandbox = _sandbox(
        tmp_path,
        environment={"SANDBOX_MARKER": "sandbox-only"},
        commands=[
            _sh('printf "%s|%s" "${LOOPFORGE_HOST_MARKER:-unset}" "${SANDBOX_MARKER:-unset}"')
        ],
    )

    result = sandbox.run("sh")

    assert result.succeeded is True
    assert result.stdout == "unset|sandbox-only"


@_REQUIRES_RLIMIT_AS
def test_run_without_configured_environment_exposes_no_host_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOPFORGE_HOST_MARKER", "host-secret")
    sandbox = _sandbox(tmp_path, commands=[_sh('printf "[%s]" "${LOOPFORGE_HOST_MARKER:-unset}"')])

    result = sandbox.run("sh")

    assert result.succeeded is True
    assert result.stdout == "[unset]"


@_REQUIRES_RLIMIT_AS
def test_run_kills_command_exceeding_wall_clock_timeout(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path,
        commands=[_command("sleep", ("/bin/sleep", "30"), timeout_seconds=20.0)],
    )

    started = time.monotonic()
    with pytest.raises(SandboxTimeoutError, match="exceeded"):
        sandbox.run("sleep", timeout_seconds=0.5)

    assert time.monotonic() - started < 10


def test_run_rejects_non_positive_timeout_override(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxPolicyError, match="must be positive"):
        sandbox.run("sh", timeout_seconds=0)


@_REQUIRES_RLIMIT_AS
def test_run_bounds_stdout_capture(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("a" * 64 + "b" * 192, encoding="utf-8")
    sandbox = _sandbox(
        tmp_path,
        limits=SandboxLimits(max_output_bytes=64),
        commands=[_command("cat", ("/bin/cat", "big.txt"))],
    )

    result = sandbox.run("cat")

    assert result.stdout == "a" * 64
    assert "b" not in result.stdout


@_REQUIRES_RLIMIT_AS
def test_run_bounds_stderr_capture(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("e" * 128, encoding="utf-8")
    sandbox = _sandbox(
        tmp_path,
        limits=SandboxLimits(max_output_bytes=32),
        commands=[_sh("/bin/cat big.txt >&2")],
    )

    result = sandbox.run("sh")

    assert result.stderr == "e" * 32
    assert result.stdout == ""


def test_capabilities_report_no_strong_isolation(tmp_path: Path) -> None:
    capabilities = _sandbox(tmp_path).capabilities

    assert capabilities.file_api_confined is True
    assert capabilities.symlink_protected is True
    assert capabilities.environment_filtered is True
    assert capabilities.process_timeout is True
    assert capabilities.resource_limits is True
    assert capabilities.output_limited is True
    assert capabilities.process_filesystem_isolated is False
    assert capabilities.network_isolated is False
    assert capabilities.kernel_isolated is False

    capabilities.require(SandboxRequirements(file_api_confined=True, symlink_protected=True))
    with pytest.raises(ValueError, match="network_isolated"):
        capabilities.require(SandboxRequirements(network_isolated=True))
    with pytest.raises(ValueError, match="kernel_isolated"):
        capabilities.require(SandboxRequirements(kernel_isolated=True))


@example(name="note.txt", content="plain deterministic example")
@given(name=_SAFE_COMPONENT, content=st.text(max_size=256))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_write_read_round_trip_property(name: str, content: str) -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        sandbox = _sandbox(root)

        sandbox.write_text(name, content)

        assert sandbox.read_text(name) == content
        assert (root / name).read_bytes() == content.encode("utf-8")
        assert not list(root.glob(".loopforge-*"))


@given(
    head=st.lists(_SAFE_COMPONENT, max_size=2),
    tail=st.lists(_SAFE_COMPONENT, max_size=2),
)
@settings(derandomize=True, max_examples=25, deadline=None)
def test_dotdot_paths_are_rejected_without_side_effects(head: list[str], tail: list[str]) -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        sandbox = _sandbox(root)
        relative = "/".join([*head, "..", *tail])

        with pytest.raises(SandboxPathError):
            sandbox.read_text(relative)
        with pytest.raises(SandboxPathError):
            sandbox.write_text(relative, "payload")
        assert list(root.rglob("*")) == []
