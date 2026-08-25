from __future__ import annotations

import dataclasses
from dataclasses import fields

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from loopforge.domain.security import (
    SandboxCapabilities,
    SandboxRequirements,
    TrustClass,
)
from loopforge.ports.sandbox import (
    SandboxCommandResult,
    SandboxError,
    SandboxPathError,
    SandboxPolicyError,
    SandboxPort,
    SandboxTimeoutError,
)

EXPECTED_FIELDS = (
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
REQUIREMENT_FIELDS = tuple(field.name for field in fields(SandboxRequirements))
CAPABILITY_FIELDS = tuple(field.name for field in fields(SandboxCapabilities))


def _capabilities(*unsupported: str) -> SandboxCapabilities:
    return SandboxCapabilities(
        file_api_confined="file_api_confined" not in unsupported,
        symlink_protected="symlink_protected" not in unsupported,
        environment_filtered="environment_filtered" not in unsupported,
        process_timeout="process_timeout" not in unsupported,
        resource_limits="resource_limits" not in unsupported,
        output_limited="output_limited" not in unsupported,
        process_filesystem_isolated="process_filesystem_isolated" not in unsupported,
        network_isolated="network_isolated" not in unsupported,
        kernel_isolated="kernel_isolated" not in unsupported,
    )


def _requirements(*required: str) -> SandboxRequirements:
    assert set(required) <= set(EXPECTED_FIELDS)
    return dataclasses.replace(SandboxRequirements(), **dict.fromkeys(required, True))


class _StaticSandbox:
    def __init__(self) -> None:
        self.writes: dict[str, str] = {}

    @property
    def capabilities(self) -> SandboxCapabilities:
        return _capabilities()

    def read_text(self, relative_path: str) -> str:
        return f"content:{relative_path}"

    def write_text(self, relative_path: str, content: str) -> None:
        if not relative_path.strip():
            msg = "relative_path cannot be empty"
            raise SandboxPathError(msg)
        self.writes[relative_path] = content

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        if timeout_seconds is not None and timeout_seconds <= 0:
            msg = "timeout_seconds must be positive"
            raise SandboxPolicyError(msg)
        return SandboxCommandResult(
            exit_code=0,
            stdout=command_name,
            stderr="",
            succeeded=True,
        )


def test_trust_class_vocabulary_is_stable() -> None:
    assert {member.value for member in TrustClass} == {
        "runtime_policy",
        "authorized_human",
        "deterministic_observation",
        "external_evidence",
        "model_inference",
        "untrusted_content",
    }
    for member in TrustClass:
        assert TrustClass(str(member)) is member


def test_requirement_vocabulary_matches_capability_vocabulary() -> None:
    assert REQUIREMENT_FIELDS == EXPECTED_FIELDS
    assert CAPABILITY_FIELDS == EXPECTED_FIELDS


def test_sandbox_requirements_default_to_no_isolation() -> None:
    requirements = SandboxRequirements()
    for name in REQUIREMENT_FIELDS:
        assert getattr(requirements, name) is False


def test_require_with_no_arguments_imposes_no_requirements() -> None:
    capabilities = _capabilities(*EXPECTED_FIELDS)
    assert capabilities.require() is None
    assert capabilities.require(None) is None


def test_require_accepts_empty_requirements_on_capability_poor_sandbox() -> None:
    capabilities = _capabilities(*EXPECTED_FIELDS)
    assert capabilities.require(SandboxRequirements()) is None


def test_require_accepts_fully_capable_sandbox_for_all_requirements() -> None:
    capabilities = _capabilities()
    assert capabilities.require(_requirements(*EXPECTED_FIELDS)) is None


@pytest.mark.parametrize("required", EXPECTED_FIELDS)
def test_require_allows_each_supported_capability(required: str) -> None:
    capabilities = _capabilities()
    assert capabilities.require(_requirements(required)) is None


@pytest.mark.parametrize("missing", EXPECTED_FIELDS)
def test_require_fails_closed_on_each_unsupported_capability(missing: str) -> None:
    capabilities = _capabilities(missing)
    with pytest.raises(ValueError, match=missing):
        capabilities.require(_requirements(missing))


def test_require_reports_every_missing_capability_in_field_order() -> None:
    capabilities = _capabilities("file_api_confined", "network_isolated", "kernel_isolated")
    requirements = _requirements(*EXPECTED_FIELDS)

    with pytest.raises(ValueError, match="does not satisfy required capabilities") as exc_info:
        capabilities.require(requirements)

    message = str(exc_info.value)
    missing_part = message.split(": ", 1)[1]
    assert missing_part == "file_api_confined, network_isolated, kernel_isolated"


def test_require_ignores_capability_gaps_the_workload_does_not_require() -> None:
    capabilities = _capabilities("kernel_isolated", "network_isolated")
    assert capabilities.require(_requirements("file_api_confined")) is None


def test_require_rejects_mixing_requirements_value_with_keywords() -> None:
    capabilities = _capabilities()
    with pytest.raises(ValueError, match="not both"):
        capabilities.require(SandboxRequirements(), file_api_confined=True)


def test_require_accepts_supported_legacy_keyword_requirements() -> None:
    capabilities = _capabilities("network_isolated")
    assert capabilities.require(file_api_confined=True) is None


def test_require_fails_closed_on_unsupported_legacy_keyword_requirement() -> None:
    capabilities = _capabilities("network_isolated")
    with pytest.raises(ValueError, match="network_isolated"):
        capabilities.require(network_isolated=True)


def test_require_rejects_unknown_legacy_keyword_requirement() -> None:
    capabilities = _capabilities()
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        capabilities.require(airgap=True)


@given(
    required=st.fixed_dictionaries({name: st.booleans() for name in REQUIREMENT_FIELDS}),
    supported=st.fixed_dictionaries({name: st.booleans() for name in CAPABILITY_FIELDS}),
)
@example(
    required=dict.fromkeys(REQUIREMENT_FIELDS, False),
    supported=dict.fromkeys(CAPABILITY_FIELDS, False),
)
@example(
    required=dict.fromkeys(REQUIREMENT_FIELDS, True),
    supported=dict.fromkeys(CAPABILITY_FIELDS, True),
)
def test_require_fails_exactly_when_a_requirement_is_unsupported(
    required: dict[str, bool], supported: dict[str, bool]
) -> None:
    capabilities = dataclasses.replace(_capabilities(*EXPECTED_FIELDS), **supported)
    requirements = dataclasses.replace(SandboxRequirements(), **required)
    expected_missing = [
        name for name in REQUIREMENT_FIELDS if required[name] and not supported[name]
    ]

    if expected_missing:
        with pytest.raises(ValueError, match="does not satisfy required capabilities") as exc_info:
            capabilities.require(requirements)
        assert str(exc_info.value).endswith(", ".join(expected_missing))
    else:
        assert capabilities.require(requirements) is None


def test_sandbox_security_dataclasses_are_frozen() -> None:
    requirements = SandboxRequirements()
    capabilities = _capabilities()
    with pytest.raises(dataclasses.FrozenInstanceError):
        requirements.file_api_confined = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        capabilities.kernel_isolated = False  # type: ignore[misc]


def test_sandbox_error_hierarchy_distinguishes_policy_from_timeout() -> None:
    assert issubclass(SandboxError, RuntimeError)
    assert issubclass(SandboxPolicyError, SandboxError)
    assert issubclass(SandboxPathError, SandboxPolicyError)
    assert issubclass(SandboxTimeoutError, SandboxError)
    assert not issubclass(SandboxTimeoutError, SandboxPolicyError)


def test_sandbox_errors_propagate_through_base_classes() -> None:
    path_msg = "path escapes the workspace"
    with pytest.raises(SandboxPolicyError, match="escapes the workspace"):
        raise SandboxPathError(path_msg)

    timeout_msg = "command exceeded its timeout"
    with pytest.raises(SandboxError, match="exceeded its timeout"):
        raise SandboxTimeoutError(timeout_msg)


def test_sandbox_command_result_is_a_frozen_value_object() -> None:
    result = SandboxCommandResult(exit_code=1, stdout="out", stderr="err", succeeded=False)
    assert result.exit_code == 1
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert not result.succeeded

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.exit_code = 0  # type: ignore[misc]

    duplicate = SandboxCommandResult(exit_code=1, stdout="out", stderr="err", succeeded=False)
    assert len({result, duplicate}) == 1


def test_static_sandbox_satisfies_sandbox_port() -> None:
    concrete = _StaticSandbox()
    sandbox: SandboxPort = concrete

    sandbox.capabilities.require(SandboxRequirements(file_api_confined=True))
    assert sandbox.read_text("src/main.py") == "content:src/main.py"
    sandbox.write_text("src/main.py", "print('hi')")
    assert concrete.writes == {"src/main.py": "print('hi')"}

    result = sandbox.run("pytest", timeout_seconds=5.0)
    assert result.succeeded
    assert result.exit_code == 0
    assert result.stdout == "pytest"
    assert result.stderr == ""


def test_sandbox_port_run_declares_no_default_implementation() -> None:
    run_without_default = SandboxPort.run
    assert run_without_default(_StaticSandbox(), "pytest") is None  # pyright: ignore[reportAbstractUsage]
