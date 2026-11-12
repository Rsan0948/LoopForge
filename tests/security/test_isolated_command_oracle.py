"""The candidate and its acceptance decision never share a filesystem or interpreter."""

from pathlib import Path

import pytest

from loopforge.adapters.container_sandbox import (
    CommandOracle,
    ContainerSandbox,
    ContainerSandboxConfig,
)
from loopforge.adapters.local_sandbox import CommandSpec
from tests.security.test_container_sandbox import (
    _REQUIRES_DOCKER,  # pyright: ignore[reportPrivateUsage]
)


@_REQUIRES_DOCKER
@pytest.mark.parametrize("correct", [True, False])
def test_candidate_cannot_replace_or_import_the_oracle(tmp_path: Path, correct: bool) -> None:
    (tmp_path / "candidate.py").write_text("VALUE = 3\n")
    producer = (
        "import inspect, json, os\n"
        "for frame in inspect.stack():\n"
        "    frame.frame.f_globals['expected'] = 0\n"
        "print(json.dumps({'value': " + ("3" if correct else "0") + "}))\n"
    )
    oracle = (
        "import json, sys, pathlib\n"
        "assert not pathlib.Path('/workspace/candidate.py').exists()\n"
        "observed = json.loads(sys.argv[1])\n"
        "assert observed['exit_code'] == 0\n"
        "assert json.loads(observed['stdout'])['value'] == 3\n"
        "print('oracle-complete')\n"
    )
    config = ContainerSandboxConfig(
        image="python:3.12-alpine",
        commands=(
            CommandSpec(
                name="check",
                argv=("/usr/local/bin/python", "-I", "-c", producer),
                timeout_seconds=15,
            ),
        ),
        oracles=(
            CommandOracle(command_name="check", argv=("/usr/local/bin/python", "-I", "-c", oracle)),
        ),
        workspace_read_only=True,
    )
    with ContainerSandbox(tmp_path, config=config) as sandbox:
        result = sandbox.run("check")
    assert result.succeeded is correct
    assert ("oracle-complete" in result.stdout) is correct


def test_oracle_cannot_reference_an_unregistered_command() -> None:
    with pytest.raises(ValueError, match="registered"):
        ContainerSandboxConfig(
            image="offline",
            commands=(),
            oracles=(CommandOracle(command_name="unregistered", argv=("/usr/local/bin/python",)),),
        )


@_REQUIRES_DOCKER
@pytest.mark.parametrize("oracle_exit", [0, 1])
def test_oracle_exit_policy_is_independent_of_producer(tmp_path: Path, oracle_exit: int) -> None:
    config = ContainerSandboxConfig(
        image="python:3.12-alpine",
        commands=(
            CommandSpec(
                name="check",
                argv=("/usr/local/bin/python", "-c", "raise SystemExit(1)"),
                timeout_seconds=15,
                allowed_exit_codes=frozenset({1}),
            ),
        ),
        oracles=(
            CommandOracle(
                command_name="check",
                argv=("/usr/local/bin/python", "-c", f"raise SystemExit({oracle_exit})"),
            ),
        ),
    )
    with ContainerSandbox(tmp_path, config=config) as sandbox:
        result = sandbox.run("check")
    assert result.exit_code == oracle_exit
    assert result.succeeded is (oracle_exit == 0)
