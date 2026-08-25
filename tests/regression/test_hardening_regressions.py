"""Regression tests for hardening fixes made during the hygiene pass.

Each test pins a defect that was found and fixed; they exist so the defects
cannot silently return.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import pytest

from loopforge.adapters import _sandbox_exec
from loopforge.adapters.json_events import (
    JsonEventCodec,
    UnsupportedEventSchemaError,
)
from loopforge.adapters.local_sandbox import CommandSpec, ConstrainedLocalSandbox
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import RetryScheduled, ToolFailed
from loopforge.domain.reliability import (
    ReliabilityPolicy,
    ToolFailureClass,
)
from loopforge.domain.tooling import (
    ApprovalClass,
    DataSensitivity,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import (
    ActionId,
    BudgetLimit,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    UsageDelta,
)
from loopforge.ports.sandbox import SandboxError
from loopforge.ports.tools import ToolExecutionRequest

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _metadata(*, timeout_seconds: float = 5.0) -> ToolMetadata:
    return ToolMetadata(
        name="tool",
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=timeout_seconds,
        sensitivity=DataSensitivity.INTERNAL,
    )


# --- Non-finite numbers must never pass validation ---------------------------
# Previously `nan <= 0` / `inf <= 0` are both False, so NaN/Infinity silently
# passed positivity checks and would disable or corrupt budgets and timeouts.


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_budget_limit_rejects_non_finite_cost(bad: float) -> None:
    with pytest.raises(ValueError, match="max_cost_usd must be finite"):
        BudgetLimit(max_cost_usd=bad, max_iterations=5)


def test_budget_limit_rejects_non_finite_elapsed() -> None:
    with pytest.raises(ValueError, match="max_elapsed_seconds must be finite"):
        BudgetLimit(max_cost_usd=1.0, max_iterations=5, max_elapsed_seconds=math.inf)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_usage_delta_rejects_non_finite_cost(bad: float) -> None:
    with pytest.raises(ValueError, match="cost_usd must be finite"):
        UsageDelta(cost_usd=bad)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_tool_metadata_rejects_non_finite_timeout(bad: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be finite"):
        _metadata(timeout_seconds=bad)


def _request(*, timeout_seconds: float) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        proposal=ActionProposal(action_id=ActionId("a1"), tool_name="tool", arguments={}),
        attempt=1,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_tool_execution_request_rejects_non_finite_timeout(bad: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be finite"):
        _request(timeout_seconds=bad)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_retry_scheduled_rejects_non_finite_delay(bad: float) -> None:
    with pytest.raises(ValueError, match="retry delay must be finite"):
        RetryScheduled(
            event_id=EventId("e1"),
            run_id=RunId("r1"),
            occurred_at=NOW,
            sequence=1,
            action_id=ActionId("a1"),
            next_attempt=2,
            delay_seconds=bad,
            reason_code="RETRY_TRANSIENT_FAILURE",
        )


# --- Codec strict-schema hardening (AGENTS.md rule 7) ------------------------
# Previously: schema_version `true` passed (`True == 1`), score `true` decoded
# to 1.0, non-string caused_by was silently dropped, non-string failure_class
# fell into the legacy decode path, and NaN/Infinity survived JSON decoding.


def _base_fields() -> dict[str, object]:
    return {
        "event_id": "e1",
        "run_id": "r1",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "sequence": 1,
    }


def _envelope(event_type: str, event: dict[str, object], *, version: object = 1) -> str:
    return json.dumps({"schema_version": version, "event_type": event_type, "event": event})


def test_decode_rejects_boolean_schema_version() -> None:
    codec = JsonEventCodec()
    payload = _envelope("RunStarted", {**_base_fields(), "objective": "x"}, version=True)
    with pytest.raises(UnsupportedEventSchemaError):
        codec.decode(payload)


def test_decode_rejects_boolean_score() -> None:
    codec = JsonEventCodec()
    payload = _envelope("VerificationFailed", {**_base_fields(), "summary": "s", "score": True})
    with pytest.raises(TypeError, match="score must be numeric or null"):
        codec.decode(payload)


def test_decode_rejects_non_finite_score() -> None:
    codec = JsonEventCodec()
    payload = _envelope("VerificationFailed", {**_base_fields(), "summary": "s", "score": math.nan})
    with pytest.raises(ValueError, match="score must be finite"):
        codec.decode(payload)


def test_decode_rejects_non_string_caused_by() -> None:
    codec = JsonEventCodec()
    event = {**_base_fields(), "objective": "x", "caused_by": 7}
    with pytest.raises(TypeError, match="caused_by must be a string or null"):
        codec.decode(_envelope("RunStarted", event))


def test_decode_rejects_non_string_failure_class() -> None:
    codec = JsonEventCodec()
    event = {
        **_base_fields(),
        "action_id": "a1",
        "error_code": "E",
        "error_message": "m",
        "failure_class": 5,
    }
    with pytest.raises(TypeError, match="failure_class must be a string or null"):
        codec.decode(_envelope("ToolFailed", event))


def test_decode_rejects_non_finite_delay_seconds() -> None:
    codec = JsonEventCodec()
    event = {
        **_base_fields(),
        "action_id": "a1",
        "next_attempt": 2,
        "delay_seconds": math.inf,
        "reason_code": "RETRY_TRANSIENT_FAILURE",
    }
    with pytest.raises(ValueError, match="delay_seconds must be finite"):
        codec.decode(_envelope("RetryScheduled", event))


def test_decode_legacy_tool_failed_without_failure_class_still_works() -> None:
    codec = JsonEventCodec()
    event = {
        **_base_fields(),
        "action_id": "a1",
        "error_code": "E",
        "error_message": "m",
        "retryable": True,
    }
    decoded = codec.decode(_envelope("ToolFailed", event))
    assert isinstance(decoded, ToolFailed)
    assert decoded.failure_class is ToolFailureClass.TRANSIENT


# --- Dead retry branch removal ----------------------------------------------
# The TRANSIENT_ONLY guard in retry_decision was unreachable because PERMANENT
# returned earlier. The surviving, more specific reason code is pinned here.


def test_transient_only_tool_permanent_failure_reports_permanent() -> None:
    policy = ReliabilityPolicy()
    metadata = replace(_metadata(), retry=RetryClass.TRANSIENT_ONLY)
    decision = policy.retry_decision(
        metadata=metadata,
        failure_class=ToolFailureClass.PERMANENT,
        attempt=1,
        action_id=ActionId("a1"),
    )
    assert not decision.should_retry
    assert decision.reason_code == "RETRY_FAILURE_PERMANENT"


# --- Sandbox launcher failure protocol ---------------------------------------
# Previously an rlimit rejection crashed the launcher with a bare traceback and
# exit code 1, indistinguishable from a workload failure (and reportable as
# success under allowed_exit_codes={1}). Launcher failures now exit 97 with a
# stderr marker and surface as SandboxError.


def test_launcher_rejects_malformed_args() -> None:
    assert _sandbox_exec.main([]) == 64
    assert _sandbox_exec.main(["1", "2", "3"]) == 64
    assert _sandbox_exec.main(["x", "2", "3", "4", "--", "/usr/bin/true"]) == 64


def test_launcher_rlimit_failure_exits_with_marker(capsys: pytest.CaptureFixture[str]) -> None:
    # A negative rlimit makes the first setrlimit raise before any limit is
    # applied to this process, so the failure path is exercised safely.
    exit_code = _sandbox_exec.main(["-1", "1024", "128", "1024", "--", "/usr/bin/true"])
    assert exit_code == 97
    assert capsys.readouterr().err.startswith(_sandbox_exec.LAUNCHER_ERROR_MARKER)


class _FakeFailedLauncher:
    """Simulates a child process whose launcher failed before exec."""

    def __init__(self, _argv: object, *, stderr: BinaryIO, **_kwargs: object) -> None:
        stderr.write(f"{_sandbox_exec.LAUNCHER_ERROR_MARKER} resource limits rejected\n".encode())
        self.returncode = _sandbox_exec.LAUNCHER_ERROR_EXIT
        self.pid = 0

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - Popen protocol
        return self.returncode


def test_run_maps_launcher_failure_to_sandbox_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = ConstrainedLocalSandbox(
        tmp_path,
        commands=[CommandSpec(name="true", argv=("/usr/bin/true",), timeout_seconds=5.0)],
    )
    monkeypatch.setattr(subprocess, "Popen", _FakeFailedLauncher)
    with pytest.raises(SandboxError, match="sandbox launcher failed"):
        sandbox.run("true")
