from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from loopforge.adapters.sandbox_tools import SandboxCommandTools, SandboxToolBinding
from loopforge.domain.actions import ActionProposal
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.security import SandboxCapabilities, SandboxRequirements
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, Permission, RiskLevel
from loopforge.ports.sandbox import (
    SandboxCommandResult,
    SandboxError,
    SandboxPathError,
    SandboxPolicyError,
    SandboxTimeoutError,
)
from loopforge.ports.tools import ToolExecutionRequest, UnknownToolError

CAPABILITY_NAMES = (
    "file_api_confined",
    "symlink_protected",
    "environment_filtered",
    "process_timeout",
    "resource_limits",
    "output_limited",
    "process_filesystem_isolated",
    "network_isolated",
    "kernel_isolated",
)


class FakeSandbox:
    """SandboxPort stub with scripted run outcomes and recorded calls."""

    def __init__(
        self,
        capabilities: SandboxCapabilities,
        outcomes: list[SandboxCommandResult | Exception] | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._outcomes = list(outcomes) if outcomes is not None else []
        self.run_calls: list[tuple[str, float | None]] = []

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self._capabilities

    def read_text(self, relative_path: str) -> str:
        msg = f"fake sandbox does not implement read_text({relative_path})"
        raise NotImplementedError(msg)

    def write_text(self, relative_path: str, content: str) -> None:
        msg = f"fake sandbox does not implement write_text({relative_path}, {content!r})"
        raise NotImplementedError(msg)

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        self.run_calls.append((command_name, timeout_seconds))
        if not self._outcomes:
            msg = "sandbox.run called without a scripted outcome"
            raise AssertionError(msg)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _capabilities(**overrides: bool) -> SandboxCapabilities:
    values = dict.fromkeys(CAPABILITY_NAMES, False) | overrides
    return SandboxCapabilities(
        file_api_confined=values["file_api_confined"],
        symlink_protected=values["symlink_protected"],
        environment_filtered=values["environment_filtered"],
        process_timeout=values["process_timeout"],
        resource_limits=values["resource_limits"],
        output_limited=values["output_limited"],
        process_filesystem_isolated=values["process_filesystem_isolated"],
        network_isolated=values["network_isolated"],
        kernel_isolated=values["kernel_isolated"],
    )


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
    command_name: str = "run-inspect",
    requirements: SandboxRequirements | None = None,
) -> SandboxToolBinding:
    return SandboxToolBinding(
        metadata=_metadata(name),
        command_name=command_name,
        requirements=requirements or SandboxRequirements(),
    )


def _request(tool_name: str = "inspect", *, timeout_seconds: float = 5.0) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        proposal=ActionProposal(ActionId("a1"), tool_name, {}),
        attempt=1,
        timeout_seconds=timeout_seconds,
    )


def test_constructs_when_sandbox_satisfies_declared_requirements() -> None:
    requirements = SandboxRequirements(file_api_confined=True, process_timeout=True)
    tools = SandboxCommandTools(
        FakeSandbox(_capabilities(file_api_confined=True, process_timeout=True)),
        [_binding(requirements=requirements)],
    )

    assert tools.metadata_for("inspect").name == "inspect"


def test_constructs_with_default_requirements_against_capability_free_sandbox() -> None:
    tools = SandboxCommandTools(FakeSandbox(_capabilities()), [_binding()])

    assert tools.metadata_for("inspect").name == "inspect"


def test_fails_closed_at_construction_when_capability_is_missing() -> None:
    sandbox = FakeSandbox(_capabilities(file_api_confined=True))
    requirements = SandboxRequirements(file_api_confined=True, network_isolated=True)

    with pytest.raises(ValueError, match="sandbox cannot satisfy tool 'inspect'") as exc_info:
        SandboxCommandTools(sandbox, [_binding(requirements=requirements)])

    assert "network_isolated" in str(exc_info.value)
    assert "file_api_confined" not in str(exc_info.value)


def test_construction_failure_names_the_unsatisfiable_tool() -> None:
    sandbox = FakeSandbox(_capabilities())
    bindings = [
        _binding("ok-tool"),
        _binding(
            "strict-tool",
            command_name="run-strict",
            requirements=SandboxRequirements(kernel_isolated=True),
        ),
    ]

    with pytest.raises(ValueError, match="strict-tool") as exc_info:
        SandboxCommandTools(sandbox, bindings)

    assert "ok-tool" not in str(exc_info.value)
    assert "kernel_isolated" in str(exc_info.value)


def test_duplicate_tool_names_are_rejected() -> None:
    sandbox = FakeSandbox(_capabilities())
    bindings = [_binding(command_name="run-a"), _binding(command_name="run-b")]

    with pytest.raises(ValueError, match="sandbox tool names must be unique"):
        SandboxCommandTools(sandbox, bindings)


def test_duplicate_names_rejected_even_when_requirements_also_unsatisfiable() -> None:
    sandbox = FakeSandbox(_capabilities())
    bindings = [
        _binding(command_name="run-a", requirements=SandboxRequirements(network_isolated=True)),
        _binding(command_name="run-b"),
    ]

    with pytest.raises(ValueError, match="sandbox tool names must be unique"):
        SandboxCommandTools(sandbox, bindings)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"name": " "}, "name cannot be empty"),
        ({"timeout_seconds": 0.0}, "timeout_seconds must be positive"),
        ({"risk": RiskLevel.LOCAL_WRITE}, "requires permission"),
        ({"idempotency": IdempotencyClass.NONE}, "idempotency=none"),
        (
            {
                "risk": RiskLevel.LOCAL_WRITE,
                "required_permission": Permission.LOCAL_WRITE,
                "side_effect": SideEffectClass.LOCAL_WRITE,
                "idempotency": IdempotencyClass.NONE,
            },
            "natural or keyed idempotency",
        ),
        (
            {"side_effect": SideEffectClass.IRREVERSIBLE},
            "irreversible tools require human approval",
        ),
    ],
)
def test_metadata_contract_violations_are_rejected(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        replace(_metadata(), **overrides)  # type: ignore[arg-type]


def test_metadata_for_returns_the_registered_contract() -> None:
    binding = _binding()
    tools = SandboxCommandTools(FakeSandbox(_capabilities()), [binding])

    assert tools.metadata_for("inspect") is binding.metadata


def test_metadata_for_unknown_tool_raises() -> None:
    tools = SandboxCommandTools(FakeSandbox(_capabilities()), [_binding()])

    with pytest.raises(UnknownToolError, match="unknown sandbox tool: missing"):
        tools.metadata_for("missing")


def test_execute_unknown_tool_fails_before_touching_the_sandbox() -> None:
    sandbox = FakeSandbox(_capabilities())
    tools = SandboxCommandTools(sandbox, [_binding()])

    with pytest.raises(UnknownToolError, match="unknown sandbox tool: ghost"):
        tools.execute(_request("ghost"))

    assert sandbox.run_calls == []


def test_execute_success_returns_ok_result_with_full_observation() -> None:
    sandbox = FakeSandbox(
        _capabilities(),
        [SandboxCommandResult(exit_code=0, stdout="all green", stderr="", succeeded=True)],
    )
    tools = SandboxCommandTools(sandbox, [_binding()])

    result = tools.execute(_request(timeout_seconds=2.5))

    assert result.ok
    assert result.observation == "exit_code=0\nstdout:\nall green\nstderr:\n"
    assert result.error_code is None
    assert result.failure_class is None
    # The sandbox receives the binding's command name, not the model-visible tool name.
    assert sandbox.run_calls == [("run-inspect", 2.5)]


def test_execute_failed_exit_maps_to_permanent_exit_failure() -> None:
    sandbox = FakeSandbox(
        _capabilities(),
        [SandboxCommandResult(exit_code=3, stdout="", stderr="boom", succeeded=False)],
    )
    tools = SandboxCommandTools(sandbox, [_binding()])

    result = tools.execute(_request())

    assert not result.ok
    assert result.error_code == "SANDBOX_EXIT_3"
    assert result.failure_class is ToolFailureClass.PERMANENT
    assert result.observation == "exit_code=3\nstdout:\n\nstderr:\nboom"


def test_execute_timeout_maps_to_transient_failure() -> None:
    sandbox = FakeSandbox(_capabilities(), [SandboxTimeoutError("timed out after 5s")])
    tools = SandboxCommandTools(sandbox, [_binding()])

    result = tools.execute(_request())

    assert not result.ok
    assert result.error_code == "SANDBOX_TIMEOUT"
    assert result.failure_class is ToolFailureClass.TRANSIENT
    assert result.observation == "timed out after 5s"


def test_execute_policy_violation_maps_to_permanent_policy_failure() -> None:
    sandbox = FakeSandbox(_capabilities(), [SandboxPolicyError("command not allowed")])
    tools = SandboxCommandTools(sandbox, [_binding()])

    result = tools.execute(_request())

    assert not result.ok
    assert result.error_code == "SANDBOX_POLICY"
    assert result.failure_class is ToolFailureClass.PERMANENT
    assert result.observation == "command not allowed"


def test_execute_path_escape_maps_to_policy_failure_not_execution_failure() -> None:
    sandbox = FakeSandbox(_capabilities(), [SandboxPathError("path escapes workspace")])
    tools = SandboxCommandTools(sandbox, [_binding()])

    result = tools.execute(_request())

    assert not result.ok
    assert result.error_code == "SANDBOX_POLICY"
    assert result.failure_class is ToolFailureClass.PERMANENT
    assert result.observation == "path escapes workspace"


def test_execute_generic_sandbox_error_maps_to_execution_failure() -> None:
    sandbox = FakeSandbox(_capabilities(), [SandboxError("executor crashed")])
    tools = SandboxCommandTools(sandbox, [_binding()])

    result = tools.execute(_request())

    assert not result.ok
    assert result.error_code == "SANDBOX_EXECUTION"
    assert result.failure_class is ToolFailureClass.PERMANENT
    assert result.observation == "executor crashed"


_CAPABILITY_STRATEGIES = {name: st.booleans() for name in CAPABILITY_NAMES}


@given(
    available=st.builds(SandboxCapabilities, **_CAPABILITY_STRATEGIES),
    required=st.builds(SandboxRequirements, **_CAPABILITY_STRATEGIES),
)
@settings(derandomize=True, max_examples=100)
def test_construction_succeeds_exactly_when_capabilities_cover_requirements(
    available: SandboxCapabilities, required: SandboxRequirements
) -> None:
    missing = [
        name
        for name in CAPABILITY_NAMES
        if getattr(required, name) and not getattr(available, name)
    ]

    if missing:
        with pytest.raises(ValueError, match="sandbox cannot satisfy tool"):
            SandboxCommandTools(FakeSandbox(available), [_binding(requirements=required)])
    else:
        tools = SandboxCommandTools(FakeSandbox(available), [_binding(requirements=required)])
        assert tools.metadata_for("inspect") is not None


@given(
    exit_code=st.integers(min_value=-64, max_value=255),
    stdout=st.text(max_size=32),
    stderr=st.text(max_size=32),
    succeeded=st.booleans(),
)
@settings(derandomize=True, max_examples=60)
def test_execute_result_reflects_command_outcome(
    exit_code: int, stdout: str, stderr: str, succeeded: bool
) -> None:
    sandbox = FakeSandbox(
        _capabilities(),
        [
            SandboxCommandResult(
                exit_code=exit_code, stdout=stdout, stderr=stderr, succeeded=succeeded
            )
        ],
    )
    tools = SandboxCommandTools(sandbox, [_binding()])

    result = tools.execute(_request())

    expected_observation = f"exit_code={exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    assert result.observation == expected_observation
    assert result.ok is succeeded
    if succeeded:
        assert result.error_code is None
        assert result.failure_class is None
    else:
        assert result.error_code == f"SANDBOX_EXIT_{exit_code}"
        assert result.failure_class is ToolFailureClass.PERMANENT
