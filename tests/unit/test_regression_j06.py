"""Exercise real OS pipes while substituting the offline workload for Docker."""

import subprocess
import sys
from pathlib import Path

import pytest

from loopforge.adapters import container_sandbox
from loopforge.adapters.local_sandbox import CommandSpec, SandboxLimits


def test_one_shot_capture_is_bounded_while_draining_both_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    popen = subprocess.Popen

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
        return popen(
            [sys.executable, "-c", "import os; os.write(1,b'x'*1000000); os.write(2,b'y'*1000000)"],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            shell=shell,
            start_new_session=start_new_session,
            close_fds=close_fds,
        )

    monkeypatch.setattr(container_sandbox.subprocess, "Popen", workload)
    sandbox = container_sandbox.ContainerSandbox(
        tmp_path,
        config=container_sandbox.ContainerSandboxConfig(
            image="offline",
            commands=(CommandSpec(name="check", argv=("/bin/true",), timeout_seconds=5),),
            limits=SandboxLimits(max_output_bytes=1024),
        ),
    )
    result = sandbox.run("check")
    assert result.succeeded
    assert len(result.stdout.encode()) <= 1024
    assert len(result.stderr.encode()) <= 1024
    assert result.output_truncated
