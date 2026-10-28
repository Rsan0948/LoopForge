"""Scratch execution status does not grant acceptance or erase negative controls."""

import json

import pytest

from loopforge.adapters.execution_observation import execution_observation
from loopforge.ports.sandbox import SandboxCommandResult

MARKERS = ("Traceback (most recent call last):", "Uncaught Error")


@pytest.mark.parametrize(
    ("exit_code", "stderr", "status"),
    [
        (0, "", "zero_exit"),
        (1, "expected refusal", "nonzero_exit"),
        (1, "Traceback (most recent call last):\nRuntimeError", "exception_partial_output"),
        (1, "Uncaught Error: boom\n    at main (cli.js:3:9)", "exception_partial_output"),
        (0, "Traceback (most recent call last):", "zero_exit"),
    ],
)
def test_execution_status_is_separate_from_measurement(
    exit_code: int,
    stderr: str,
    status: str,
) -> None:
    rendered = execution_observation(
        SandboxCommandResult(
            exit_code=exit_code,
            stdout="PASS",
            stderr=stderr,
            succeeded=exit_code == 0,
        ),
        crash_markers=MARKERS,
    )
    header = json.loads(rendered.splitlines()[1])
    assert header["script_status"] == status
    assert header["measurement_status"] == "not_verified"
    assert "stdout:\nPASS" in rendered


def test_oversized_output_reports_omission() -> None:
    rendered = execution_observation(
        SandboxCommandResult(
            exit_code=0,
            stdout="x" * 6000,
            stderr="y" * 4000,
            succeeded=True,
        )
    )
    header = json.loads(rendered.splitlines()[1])
    assert header["stdout_omitted_characters"] == 2000
    assert header["stderr_omitted_characters"] == 1000
    assert len(rendered) < 8000


def test_without_markers_a_nonzero_exit_is_never_guessed_to_be_a_crash() -> None:
    # The core knows no language: a Python traceback is just stderr unless the caller
    # says how its workloads crash.
    rendered = execution_observation(
        SandboxCommandResult(
            exit_code=1,
            stdout="",
            stderr="Traceback (most recent call last):\nRuntimeError",
            succeeded=False,
        )
    )
    assert json.loads(rendered.splitlines()[1])["script_status"] == "nonzero_exit"
