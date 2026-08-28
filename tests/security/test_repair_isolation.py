"""Security tests for the repair workload's isolation and authority boundaries.

Covers fail-closed capability binding (AGENTS.md rules 13-15) and the rule-16
guarantee that fixture/repository content can never widen code-owned
requirements, acceptance criteria, allowlists, or verifier truth.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.container_sandbox import ContainerSandbox, ContainerSandboxConfig
from loopforge.adapters.local_sandbox import ConstrainedLocalSandbox
from loopforge.adapters.sandbox_tools import SandboxCommandTools
from loopforge.adapters.scripted import FixedClock
from loopforge.adapters.telemetry import NoOpTelemetry, TelemetrySandbox
from loopforge.domain.security import SandboxRequirements
from loopforge.entrypoints.repair import repair_command_bindings, repair_command_specs
from loopforge.ports.sandbox import SandboxPolicyError
from loopforge.workloads.fixtures import adder_fixture, adder_repair_task
from loopforge.workloads.repair import UNTRUSTED_REPAIR_REQUIREMENTS

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _container_sandbox(root: Path) -> ContainerSandbox:
    task = adder_repair_task(executable="/usr/local/bin/python")
    return ContainerSandbox(
        root,
        config=ContainerSandboxConfig(
            image="python:3.12-alpine",
            commands=tuple(repair_command_specs(task.commands)),
            environment={},
        ),
    )


def test_untrusted_repair_binding_fails_closed_on_local_sandbox(tmp_path: Path) -> None:
    task = adder_repair_task(executable="/usr/bin/python3")
    sandbox = ConstrainedLocalSandbox(
        tmp_path, commands=repair_command_specs(task.commands), environment={}
    )

    with pytest.raises(ValueError, match="process_filesystem_isolated"):
        SandboxCommandTools(
            sandbox,
            repair_command_bindings(task.commands, requirements=UNTRUSTED_REPAIR_REQUIREMENTS),
        )
    with pytest.raises(ValueError, match="network_isolated"):
        SandboxCommandTools(
            sandbox,
            repair_command_bindings(task.commands, requirements=UNTRUSTED_REPAIR_REQUIREMENTS),
        )


def test_untrusted_repair_binding_accepts_container_capabilities(tmp_path: Path) -> None:
    task = adder_repair_task(executable="/usr/local/bin/python")
    sandbox = _container_sandbox(tmp_path)

    tools = SandboxCommandTools(
        sandbox,
        repair_command_bindings(task.commands, requirements=UNTRUSTED_REPAIR_REQUIREMENTS),
    )

    assert tools.tool_names == ("run_tests", "build")


def test_telemetry_wrapping_preserves_untrusted_binding(tmp_path: Path) -> None:
    task = adder_repair_task(executable="/usr/local/bin/python")
    sandbox = TelemetrySandbox(_container_sandbox(tmp_path), NoOpTelemetry(), clock=FixedClock(NOW))

    tools = SandboxCommandTools(
        sandbox,
        repair_command_bindings(task.commands, requirements=UNTRUSTED_REPAIR_REQUIREMENTS),
    )

    assert set(tools.tool_names) == {"run_tests", "build"}


def test_kernel_isolation_stays_unsatisfiable_against_container(tmp_path: Path) -> None:
    task = adder_repair_task(executable="/usr/local/bin/python")
    sandbox = _container_sandbox(tmp_path)

    with pytest.raises(ValueError, match="kernel_isolated"):
        SandboxCommandTools(
            sandbox,
            repair_command_bindings(
                task.commands, requirements=SandboxRequirements(kernel_isolated=True)
            ),
        )


def test_requirements_vocabulary_cannot_be_widened_by_fixture_content() -> None:
    # Rule 14/16: fixture repositories contribute file bytes only. The
    # code-owned requirements contract and the fixture definition share no
    # fields, so repository content has no channel to widen isolation demands.
    requirement_fields = {field.name for field in fields(UNTRUSTED_REPAIR_REQUIREMENTS)}
    assert requirement_fields == {
        "file_api_confined",
        "symlink_protected",
        "environment_filtered",
        "process_timeout",
        "resource_limits",
        "output_limited",
        "process_filesystem_isolated",
        "network_isolated",
        "kernel_isolated",
    }
    assert UNTRUSTED_REPAIR_REQUIREMENTS.process_filesystem_isolated
    assert UNTRUSTED_REPAIR_REQUIREMENTS.network_isolated
    assert not UNTRUSTED_REPAIR_REQUIREMENTS.kernel_isolated

    fixture = adder_fixture()
    assert all("__pycache__" not in item.path for item in fixture.files)
    assert not any(item.path.startswith("/") for item in fixture.files)


def test_acceptance_criteria_stay_code_owned_after_wiring(tmp_path: Path) -> None:
    task = adder_repair_task(executable="/usr/bin/python3")
    criteria = task.acceptance

    assert criteria.required_commands == ("run_tests",)
    assert criteria.patch.require_change
    assert criteria.patch.allowed_prefixes == ("adder.py",)
    assert criteria.patch.max_changed_files == 1
    # The sandbox command allowlist contains exactly the predefined commands;
    # neither fixture files nor model output can extend it.
    specs = repair_command_specs(task.commands)
    assert {spec.name for spec in specs} == {"run_tests", "build"}
    sandbox = ConstrainedLocalSandbox(tmp_path, commands=specs, environment={})
    with pytest.raises(SandboxPolicyError, match="command is not allowlisted"):
        sandbox.run("fixture_supplied_command")


def test_model_output_cannot_define_tool_metadata(tmp_path: Path) -> None:
    # Rule 4: binding metadata is constructed from the code-owned command
    # definitions only; there is no path for model/repository content to set
    # risk, permission, retry, idempotency, approval, or timeout fields.
    task = adder_repair_task(executable="/usr/bin/python3")
    bindings = repair_command_bindings(task.commands, requirements=SandboxRequirements())
    for binding, command in zip(bindings, task.commands, strict=True):
        assert binding.metadata.name == command.name
        assert binding.metadata.timeout_seconds == command.timeout_seconds
        assert binding.requirements == SandboxRequirements()
