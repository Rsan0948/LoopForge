from __future__ import annotations

import os
import selectors
import stat
import subprocess
import time
from pathlib import Path

import pytest
from test_container_sandbox import (  # pyright: ignore[reportPrivateUsage]
    _REQUIRES_DOCKER,  # pyright: ignore[reportPrivateUsage]
    _TEST_IMAGE,  # pyright: ignore[reportPrivateUsage]
)

from loopforge.adapters import container_session
from loopforge.adapters.container_sandbox import ContainerSandboxConfig
from loopforge.adapters.container_session import ContainerSession, ContainerSessionConfig
from loopforge.adapters.local_sandbox import SandboxLimits
from loopforge.ports.sandbox import SandboxError, SandboxPolicyError, SandboxTimeoutError

# A stand-in Docker CLI: records every call, and runs FAKE_EXEC for `docker exec`.
_FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  run) [ -n "$FAKE_RUN_FAIL" ] && { echo "docker: no such image" >&2; exit 125; }; exit 0 ;;
  exec) eval "$FAKE_EXEC" ;;
  kill) exit 0 ;;
esac
"""


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    docker = tmp_path / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_EXEC", "echo out; echo err >&2; exit 0")
    (tmp_path / "work").mkdir()
    return docker


def _session(
    tmp_path: Path,
    docker: Path | str = "docker",
    *,
    command_timeout_seconds: int = 120,
    max_session_seconds: int = 3600,
    max_output_bytes: int | None = None,
) -> ContainerSession:
    return ContainerSession(
        tmp_path / "work",
        config=ContainerSessionConfig(
            sandbox=ContainerSandboxConfig(
                image=_TEST_IMAGE,
                commands=(),
                docker_executable=str(docker),
                workspace_read_only=True,
                limits=SandboxLimits()
                if max_output_bytes is None
                else SandboxLimits(max_output_bytes=max_output_bytes),
            ),
            command_timeout_seconds=command_timeout_seconds,
            max_session_seconds=max_session_seconds,
        ),
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def test_the_session_starts_lazily_once_with_the_hardened_contract(
    tmp_path: Path, fake: Path
) -> None:
    session = _session(tmp_path, fake, max_session_seconds=900)
    assert session.active_container is None
    assert _calls(tmp_path) == []
    first = session.run("echo hi")
    session.run("echo again")
    calls = _calls(tmp_path)
    starts = [c for c in calls if c.startswith("run ")]
    assert len(starts) == 1
    start = starts[0].split()
    for flag in (
        "--detach",
        "--rm",
        "--read-only",
        "--init",
        "no-new-privileges",
        "ALL",
        "cpu=900:900",
    ):
        assert flag in start
    assert start[start.index("--network") + 1] == "none"
    assert start[-3:] == [_TEST_IMAGE, "sleep", "900"]
    assert any("readonly" in part for part in start)
    assert first.stdout == "out\n"
    assert first.stderr == "err\n"
    assert first.succeeded
    assert not first.output_truncated


def test_each_command_runs_in_the_named_session_under_a_kill_timeout(
    tmp_path: Path, fake: Path
) -> None:
    session = _session(tmp_path, fake, command_timeout_seconds=90)
    session.run("python app.py &", timeout_seconds=30)
    session.run("curl localhost:8000", timeout_seconds=500)
    name = session.active_container
    assert name is not None
    execs = [c for c in _calls(tmp_path) if c.startswith("exec ")]
    assert execs == [
        f"exec {name} timeout -s KILL 30 /bin/sh -c python app.py &",
        # A requested bound never exceeds the code-owned one.
        f"exec {name} timeout -s KILL 90 /bin/sh -c curl localhost:8000",
    ]


@pytest.mark.parametrize("command", ["", "   ", "x" * 8001, "echo \0"])
def test_invalid_commands_are_refused_before_any_container_starts(
    tmp_path: Path, fake: Path, command: str
) -> None:
    session = _session(tmp_path, fake)
    with pytest.raises(SandboxPolicyError):
        session.run(command)
    assert _calls(tmp_path) == []


@pytest.mark.parametrize("timeout", [0, -1, 1.5, True])
def test_a_runtime_timeout_must_be_a_positive_integer(
    tmp_path: Path, fake: Path, timeout: int
) -> None:
    with pytest.raises(SandboxPolicyError):
        _session(tmp_path, fake).run("echo hi", timeout_seconds=timeout)


def test_a_command_killed_at_its_bound_is_a_timeout_and_the_session_continues(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXEC", "sleep 1.2; exit 137")
    session = _session(tmp_path, fake)
    with pytest.raises(SandboxTimeoutError, match="session continues"):
        session.run("sleep 100", timeout_seconds=1)
    assert session.active_container is not None
    monkeypatch.setenv("FAKE_EXEC", "exit 0")
    assert session.run("true").succeeded


def test_an_early_sigkill_is_the_commands_own_exit_not_a_timeout(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXEC", "exit 137")
    result = _session(tmp_path, fake).run("allocate-too-much", timeout_seconds=60)
    assert result.exit_code == 137
    assert not result.succeeded


def test_a_command_outliving_the_host_bound_destroys_the_session(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_session, "_EXEC_GRACE_SECONDS", 0.5)
    monkeypatch.setenv("FAKE_EXEC", "sleep 30")
    session = _session(tmp_path, fake)
    with pytest.raises(SandboxTimeoutError, match="session was destroyed"):
        session.run("hang", timeout_seconds=1)
    assert session.active_container is None
    assert any(c.startswith("kill loopforge-session-") for c in _calls(tmp_path))
    with pytest.raises(SandboxPolicyError, match="closed"):
        session.run("echo hi")


def test_the_session_lifetime_bounds_every_command_and_then_ends_it(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, fake, command_timeout_seconds=120, max_session_seconds=300)
    clock = time.monotonic()
    monkeypatch.setattr(container_session.time, "monotonic", lambda: clock)
    session.run("echo hi")
    clock += 250
    session.run("echo late")
    # Only the 50 s left in the session remain for this command.
    assert _calls(tmp_path)[-1].split()[5] == "50"
    clock += 60
    with pytest.raises(SandboxTimeoutError, match="exceeded 300s"):
        session.run("echo too late")
    assert session.active_container is None


def test_close_kills_the_container_and_refuses_further_commands(tmp_path: Path, fake: Path) -> None:
    with _session(tmp_path, fake) as session:
        session.run("echo hi")
        name = session.active_container
    assert f"kill {name}" in _calls(tmp_path)
    assert f"rm --force {name}" in _calls(tmp_path)
    with pytest.raises(SandboxPolicyError, match="closed"):
        session.run("echo hi")


def test_a_session_that_never_started_closes_without_a_kill(tmp_path: Path, fake: Path) -> None:
    _session(tmp_path, fake).close()
    assert _calls(tmp_path) == []


def test_a_failed_start_surfaces_as_a_sandbox_error(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RUN_FAIL", "1")
    session = _session(tmp_path, fake)
    with pytest.raises(SandboxError, match="failed to start"):
        session.run("echo hi")
    assert session.active_container is None


@pytest.mark.parametrize(
    "limits",
    [
        {"command_timeout_seconds": 0},
        {"command_timeout_seconds": 600, "max_session_seconds": 300},
        {"max_command_chars": 0},
        {"command_timeout_seconds": True},
    ],
)
def test_session_limits_are_validated(limits: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="session limits"):
        ContainerSessionConfig(
            sandbox=ContainerSandboxConfig(image=_TEST_IMAGE, commands=()),
            **limits,  # pyright: ignore[reportArgumentType]
        )


@pytest.mark.parametrize("shell", [(), ("",), ("-c",)])
def test_the_session_shell_must_be_an_executable(tmp_path: Path, shell: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="shell"):
        ContainerSessionConfig(
            sandbox=ContainerSandboxConfig(image=_TEST_IMAGE, commands=()), shell=shell
        )


def test_capabilities_are_the_container_sandboxes(tmp_path: Path, fake: Path) -> None:
    capabilities = _session(tmp_path, fake).capabilities
    assert capabilities.network_isolated
    assert capabilities.process_filesystem_isolated
    assert not capabilities.kernel_isolated


# --- live: a real session keeps state, and its isolation holds -------------------------


def _live(tmp_path: Path, *, max_output_bytes: int = 1_000_000) -> ContainerSession:
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "work" / "app.txt").write_text("source\n")
    return ContainerSession(
        tmp_path / "work",
        config=ContainerSessionConfig(
            sandbox=ContainerSandboxConfig(
                image=_TEST_IMAGE,
                commands=(),
                workspace_read_only=True,
                user=f"{os.getuid()}:{os.getgid()}",
                limits=SandboxLimits(max_output_bytes=max_output_bytes),
            ),
            command_timeout_seconds=20,
            max_session_seconds=120,
        ),
    )


@_REQUIRES_DOCKER
def test_live_files_and_background_processes_persist_across_commands(tmp_path: Path) -> None:
    with _live(tmp_path) as session:
        session.run("echo state > /tmp/note; (sleep 60 &) ; echo started")
        assert session.run("cat /tmp/note").stdout == "state\n"
        assert "sleep 60" in session.run("ps -o args").stdout
        assert session.run("cat app.txt").stdout == "source\n"
        name = session.active_container
    gone = subprocess.run(
        ["docker", "inspect", str(name)], capture_output=True, check=False, timeout=30
    )
    assert gone.returncode != 0


@_REQUIRES_DOCKER
def test_live_session_has_no_network_and_a_read_only_workspace(tmp_path: Path) -> None:
    with _live(tmp_path) as session:
        assert not session.run("wget -q -T 3 -O- http://1.1.1.1").succeeded
        assert not session.run("echo x > /workspace/new.txt").succeeded
        assert not session.run("echo x > /etc/escape").succeeded
    assert not (tmp_path / "work" / "new.txt").exists()


@_REQUIRES_DOCKER
def test_live_a_timed_out_command_leaves_the_session_usable(tmp_path: Path) -> None:
    with _live(tmp_path) as session:
        with pytest.raises(SandboxTimeoutError, match="session continues"):
            session.run("sleep 30", timeout_seconds=2)
        assert session.run("echo alive").stdout == "alive\n"


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("output_limit", [8, 128])
def test_output_flood_is_drained_with_bounded_capture_and_explicit_truncation(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, output_limit: int
) -> None:
    session = _session(tmp_path, fake, max_output_bytes=output_limit)
    monkeypatch.setenv(
        "FAKE_EXEC",
        "i=0; while [ $i -lt 20000 ]; do printf abcdefghijklmnop; "
        f"printf ABCDEFGHIJKLMNOP >&2; i=$((i+1)); done; exit {exit_code}",
    )
    with session:
        result = session.run("flood")
    assert result.output_truncated
    assert result.exit_code == exit_code
    assert result.succeeded == (exit_code == 0)
    assert len(result.stdout.encode()) <= output_limit
    assert len(result.stderr.encode()) <= output_limit
    if output_limit >= 128:
        assert result.stdout.startswith("abcdefghijklmnop")
        assert result.stderr.startswith("ABCDEFGHIJKLMNOP")
        assert result.stdout.endswith("\n[output truncated]")
        assert result.stderr.endswith("\n[output truncated]")


def test_output_flood_cannot_bypass_the_host_timeout(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_session, "_EXEC_GRACE_SECONDS", 0.1)
    monkeypatch.setenv("FAKE_EXEC", "while :; do printf flood; printf flood >&2; done")
    session = _session(tmp_path, fake, command_timeout_seconds=1)
    with pytest.raises(SandboxTimeoutError, match="destroyed"):
        session.run("flood")
    assert session.active_container is None
    assert any(call.startswith("kill ") for call in _calls(tmp_path))


def test_command_exit_does_not_drop_buffered_output_below_the_cap(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FAKE_EXEC",
        'python3 -c \'import os; os.write(1, b"x" * 200000); os.write(2, b"y" * 200000)\'',
    )
    with _session(tmp_path, fake) as session:
        result = session.run("buffered")
    assert result.succeeded
    assert result.stdout == "x" * 200000
    assert result.stderr == "y" * 200000
    assert not result.output_truncated


def test_capture_drains_a_finished_process_even_when_selector_reports_no_events(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MissedWakeupSelector(selectors.DefaultSelector):
        def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
            del timeout  # Deliberately miss the wakeup despite readable pipes.
            return []

    monkeypatch.setattr(container_session.selectors, "DefaultSelector", MissedWakeupSelector)
    session = _session(tmp_path, fake)
    with subprocess.Popen(
        ["/bin/sh", "-c", "printf 'last stdout'; printf 'last stderr' >&2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        process.wait(timeout=2)
        captured = session._capture_output(process, 1)  # pyright: ignore[reportPrivateUsage]
    assert captured.stdout == "last stdout"
    assert captured.stderr == "last stderr"
    assert not captured.truncated


@pytest.mark.parametrize(
    "case",
    [("ff" * 8, 8, True), ("e282", 2, True), ("c3a9", 2, False), ("61626364", 4, False)],
)
def test_returned_utf8_bytes_respect_the_capture_allowance(
    tmp_path: Path,
    fake: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, int, bool],
) -> None:
    hex_bytes, limit, truncated = case
    monkeypatch.setenv(
        "FAKE_EXEC",
        "python3 -c 'import os; "
        f'os.write(1, bytes.fromhex("{hex_bytes}")); '
        f'os.write(2, bytes.fromhex("{hex_bytes}"))' + "'",
    )
    with _session(tmp_path, fake, max_output_bytes=limit) as session:
        result = session.run("bytes")
    assert result.succeeded
    assert len(result.stdout.encode()) <= limit
    assert len(result.stderr.encode()) <= limit
    assert result.output_truncated is truncated
    if not truncated:
        assert result.stdout == bytes.fromhex(hex_bytes).decode()
        assert result.stderr == bytes.fromhex(hex_bytes).decode()


def test_truncated_daemon_error_is_still_an_infrastructure_failure(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXEC", "echo 'docker: synthetic runtime failure' >&2; exit 125")
    with (
        _session(tmp_path, fake, max_output_bytes=8) as session,
        pytest.raises(SandboxError, match="container runtime failed"),
    ):
        session.run("unavailable")


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_capture_failure_reaps_the_cli_closes_pipes_and_disables_the_session(
    tmp_path: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    seen: list[subprocess.Popen[bytes]] = []

    def broken_capture(process: subprocess.Popen[bytes], timeout: float) -> None:
        assert timeout > 0
        seen.append(process)
        msg = "injected capture failure"
        raise failure(msg)

    monkeypatch.setenv("FAKE_EXEC", "while :; do :; done")
    session = _session(tmp_path, fake)
    monkeypatch.setattr(session, "_capture_output", broken_capture)
    expected = SandboxError if failure is OSError else KeyboardInterrupt
    with pytest.raises(expected, match="injected capture failure"):
        session.run("probe")
    assert len(seen) == 1
    process = seen[0]
    assert process.poll() is not None
    assert process.stdout is not None
    assert process.stderr is not None
    assert process.stdout.closed
    assert process.stderr.closed
    assert session.active_container is None
    assert any(call.startswith("kill ") for call in _calls(tmp_path))
    with pytest.raises(SandboxPolicyError, match="closed"):
        session.run("must not restart")


@_REQUIRES_DOCKER
@pytest.mark.parametrize("exit_code", [0, 7])
def test_live_output_flood_is_bounded_and_does_not_end_the_session(
    tmp_path: Path, exit_code: int
) -> None:
    with _live(tmp_path, max_output_bytes=128) as session:
        result = session.run(
            "i=0; while [ $i -lt 10000 ]; do printf abcdefghijklmnop; "
            f"printf ABCDEFGHIJKLMNOP >&2; i=$((i+1)); done; exit {exit_code}"
        )
        assert result.exit_code == exit_code
        assert result.succeeded == (exit_code == 0)
        assert result.output_truncated
        assert len(result.stdout.encode()) <= 128
        assert len(result.stderr.encode()) <= 128
        assert result.stdout.endswith("[output truncated]")
        assert result.stderr.endswith("[output truncated]")
        later = session.run("echo usable")
        assert later.succeeded
        assert later.stdout == "usable\n"
        assert not later.output_truncated
