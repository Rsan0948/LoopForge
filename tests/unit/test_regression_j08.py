import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopforge.adapters import container_sandbox
from loopforge.adapters.local_sandbox import CommandSpec
from loopforge.ports.sandbox import SandboxError


@pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError])
def test_interrupted_capture_destroys_container_and_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    killed: list[list[str]] = []
    popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def workload(  # noqa: PLR0913 - the Popen test seam
        _argv: object,
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        shell: bool,
        start_new_session: bool,
        close_fds: bool,
    ) -> subprocess.Popen[bytes]:
        child = popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            shell=shell,
            start_new_session=start_new_session,
            close_fds=close_fds,
        )
        children.append(child)
        return child

    def interrupted(*_args: object) -> None:
        message = "simulated interrupted capture"
        raise failure(message)

    def kill(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        killed.append(argv)
        children[0].terminate()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(container_sandbox.subprocess, "Popen", workload)
    monkeypatch.setattr(container_sandbox.subprocess, "run", kill)
    monkeypatch.setattr(container_sandbox, "capture_output", interrupted)
    config = container_sandbox.ContainerSandboxConfig(
        image="offline",
        commands=(CommandSpec(name="check", argv=("/bin/true",), timeout_seconds=5),),
    )
    with (
        pytest.raises(KeyboardInterrupt if failure is KeyboardInterrupt else SandboxError),
        container_sandbox.ContainerSandbox(tmp_path, config=config) as sandbox,
    ):
        sandbox.run("check")
    assert killed
    assert children[0].poll() is not None
    assert sandbox.active_container is None


def test_failed_cleanup_retains_identity_for_a_later_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loopforge.ports.sandbox import SandboxError  # noqa: PLC0415

    sandbox = container_sandbox.ContainerSandbox(
        tmp_path, config=container_sandbox.ContainerSandboxConfig(image="offline", commands=())
    )
    sandbox._active_container = "offline-container"  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(container_sandbox, "_KILL_TIMEOUT_SECONDS", 0.01)

    def unavailable(_argv: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stderr=b"daemon unavailable")

    monkeypatch.setattr(container_sandbox.subprocess, "run", unavailable)
    with pytest.raises(SandboxError, match="unconfirmed"):
        sandbox.destroy()
    assert sandbox.active_container == "offline-container"

    def killed(_argv: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(container_sandbox.subprocess, "run", killed)
    sandbox.destroy()
    assert sandbox.active_container is None


def test_oracle_cleanup_failure_remains_reachable_for_retry(tmp_path: Path) -> None:
    from loopforge.ports.sandbox import SandboxCommandResult  # noqa: PLC0415

    class InterruptedOracle(container_sandbox.ContainerSandbox):
        cleanup_attempts = 0

        def _run_command(self, spec: CommandSpec, effective_timeout: float) -> SandboxCommandResult:
            assert spec.name == "check"
            assert effective_timeout > 0
            self._active_container = "oracle-container"
            message = "oracle capture interrupted"
            raise SandboxError(message)

        def _kill_container(
            self, container_name: str, *, process: subprocess.Popen[bytes] | None
        ) -> bool:
            assert container_name == "oracle-container"
            assert process is None
            type(self).cleanup_attempts += 1
            return type(self).cleanup_attempts > 1

    config = container_sandbox.ContainerSandboxConfig(
        image="offline",
        commands=(CommandSpec(name="check", argv=("/bin/true",), timeout_seconds=5),),
        oracles=(
            container_sandbox.CommandOracle(
                command_name="check", argv=("/bin/true",), producer=False
            ),
        ),
    )
    sandbox = InterruptedOracle(tmp_path, config=config)
    with pytest.raises(SandboxError, match="unconfirmed"):
        sandbox.run("check")
    assert InterruptedOracle.cleanup_attempts == 1
    sandbox.destroy()
    assert InterruptedOracle.cleanup_attempts == 2


def test_unconfirmed_cleanup_blocks_new_execution(tmp_path: Path) -> None:
    config = container_sandbox.ContainerSandboxConfig(
        image="offline",
        commands=(CommandSpec(name="check", argv=("/bin/true",), timeout_seconds=5),),
    )
    sandbox = container_sandbox.ContainerSandbox(tmp_path, config=config)
    sandbox._active_container = "retained-container"  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(SandboxError, match="cleanup"):
        sandbox.run("check")
    assert sandbox.active_container == "retained-container"


def test_destroy_confirms_an_already_removed_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = container_sandbox.ContainerSandbox(
        tmp_path, config=container_sandbox.ContainerSandboxConfig(image="offline", commands=())
    )
    sandbox._active_container = "removed-container"  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(container_sandbox, "_KILL_TIMEOUT_SECONDS", 0.01)

    def absent(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            argv, 1, b"", b"Error response from daemon: No such container: removed-container"
        )

    monkeypatch.setattr(container_sandbox.subprocess, "run", absent)
    sandbox.destroy()
    assert sandbox.active_container is None
