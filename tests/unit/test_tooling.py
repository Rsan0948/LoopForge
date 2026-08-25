from __future__ import annotations

import dataclasses

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from loopforge.domain.actions import ActionProposal
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import (
    ApprovalClass,
    DataSensitivity,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, Permission, RiskLevel
from loopforge.ports.tools import (
    ToolContractError,
    ToolExecutionRequest,
    ToolExecutorPort,
    ToolResult,
    UnknownToolError,
)

EXPECTED_PERMISSION_BY_RISK = {
    RiskLevel.READ_ONLY: Permission.READ,
    RiskLevel.LOCAL_WRITE: Permission.LOCAL_WRITE,
    RiskLevel.EXTERNAL_WRITE: Permission.EXTERNAL_WRITE,
    RiskLevel.CRITICAL: Permission.CRITICAL,
}
SIDE_EFFECTING_CLASSES = (
    SideEffectClass.LOCAL_WRITE,
    SideEffectClass.EXTERNAL_WRITE,
    SideEffectClass.IRREVERSIBLE,
)
NON_SIDE_EFFECTING_CLASSES = (SideEffectClass.PURE, SideEffectClass.READ_ONLY)
RETRYABLE_CLASSES = (RetryClass.TRANSIENT_ONLY, RetryClass.SAFE)
WEAK_IDEMPOTENCY_CLASSES = (IdempotencyClass.NOT_APPLICABLE, IdempotencyClass.NONE)
STRONG_IDEMPOTENCY_CLASSES = (IdempotencyClass.NATURAL, IdempotencyClass.KEYED)


def _metadata(  # noqa: PLR0913
    *,
    name: str = "inspect",
    risk: RiskLevel = RiskLevel.READ_ONLY,
    required_permission: Permission = Permission.READ,
    side_effect: SideEffectClass = SideEffectClass.PURE,
    retry: RetryClass = RetryClass.NEVER,
    idempotency: IdempotencyClass = IdempotencyClass.NATURAL,
    approval: ApprovalClass = ApprovalClass.NONE,
    timeout_seconds: float = 5.0,
    sensitivity: DataSensitivity = DataSensitivity.INTERNAL,
) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=risk,
        required_permission=required_permission,
        side_effect=side_effect,
        retry=retry,
        idempotency=idempotency,
        approval=approval,
        timeout_seconds=timeout_seconds,
        sensitivity=sensitivity,
    )


def _approval_for(side_effect: SideEffectClass) -> ApprovalClass:
    if side_effect is SideEffectClass.IRREVERSIBLE:
        return ApprovalClass.REQUIRED
    return ApprovalClass.NONE


def _proposal() -> ActionProposal:
    return ActionProposal(ActionId("a1"), "inspect", {})


def _request(
    *,
    attempt: int = 1,
    timeout_seconds: float = 5.0,
    idempotency_key: str | None = None,
) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        proposal=_proposal(),
        attempt=attempt,
        timeout_seconds=timeout_seconds,
        idempotency_key=idempotency_key,
    )


class _StaticExecutor:
    def metadata_for(self, tool_name: str) -> ToolMetadata:
        if tool_name != "inspect":
            raise UnknownToolError(tool_name)
        return _metadata()

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        return ToolResult(ok=True, observation=f"ran {request.proposal.tool_name}")


def test_tooling_enum_vocabularies_are_stable() -> None:
    assert {member.value for member in SideEffectClass} == {
        "pure",
        "read_only",
        "local_write",
        "external_write",
        "irreversible",
    }
    assert {member.value for member in RetryClass} == {"never", "transient_only", "safe"}
    assert {member.value for member in IdempotencyClass} == {
        "not_applicable",
        "natural",
        "keyed",
        "none",
    }
    assert {member.value for member in ApprovalClass} == {"none", "policy_dependent", "required"}
    assert {member.value for member in DataSensitivity} == {
        "public",
        "internal",
        "sensitive",
        "secret",
    }


def test_tooling_enums_round_trip_through_strings() -> None:
    enums = (SideEffectClass, RetryClass, IdempotencyClass, ApprovalClass, DataSensitivity)
    for enum in enums:
        for member in enum:
            assert enum(str(member)) is member
            assert enum(member.value) is member


def test_every_risk_level_accepts_its_expected_permission() -> None:
    for risk, expected_permission in EXPECTED_PERMISSION_BY_RISK.items():
        metadata = _metadata(risk=risk, required_permission=expected_permission)
        assert metadata.risk is risk
        assert metadata.required_permission is expected_permission


@pytest.mark.parametrize(
    ("risk", "wrong_permission"),
    [
        (risk, permission)
        for risk, expected_permission in EXPECTED_PERMISSION_BY_RISK.items()
        for permission in Permission
        if permission is not expected_permission
    ],
)
def test_metadata_rejects_permission_mismatched_to_risk(
    risk: RiskLevel, wrong_permission: Permission
) -> None:
    expected_permission = EXPECTED_PERMISSION_BY_RISK[risk]
    with pytest.raises(ValueError, match=f"requires permission {expected_permission.value}"):
        _metadata(risk=risk, required_permission=wrong_permission)


@pytest.mark.parametrize("blank_name", ["", "   ", "\t\n"])
def test_metadata_rejects_blank_names(blank_name: str) -> None:
    with pytest.raises(ValueError, match="name cannot be empty"):
        _metadata(name=blank_name)


@given(name=st.text(alphabet=[" ", "\t", "\n", "\r"]))
@example(name="")
def test_metadata_rejects_any_whitespace_only_name(name: str) -> None:
    with pytest.raises(ValueError, match="name cannot be empty"):
        _metadata(name=name)


@given(name=st.text(min_size=1).filter(str.strip))
@example(name="inspect")
def test_metadata_accepts_any_non_blank_name(name: str) -> None:
    assert _metadata(name=name).name == name


@pytest.mark.parametrize("timeout_seconds", [0.0, -0.5, -100.0])
def test_metadata_rejects_non_positive_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        _metadata(timeout_seconds=timeout_seconds)


@given(timeout_seconds=st.floats(max_value=0.0, allow_nan=False, allow_infinity=False))
@example(timeout_seconds=0.0)
def test_metadata_rejects_any_non_positive_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        _metadata(timeout_seconds=timeout_seconds)


@given(timeout_seconds=st.floats(min_value=1e-9, max_value=1e9, allow_nan=False))
@example(timeout_seconds=5.0)
def test_metadata_accepts_any_positive_timeout(timeout_seconds: float) -> None:
    assert _metadata(timeout_seconds=timeout_seconds).timeout_seconds == timeout_seconds


@pytest.mark.parametrize("side_effect", NON_SIDE_EFFECTING_CLASSES)
def test_pure_and_read_only_tools_reject_none_idempotency(
    side_effect: SideEffectClass,
) -> None:
    with pytest.raises(ValueError, match="cannot declare idempotency=none"):
        _metadata(side_effect=side_effect, idempotency=IdempotencyClass.NONE)


@pytest.mark.parametrize("side_effect", NON_SIDE_EFFECTING_CLASSES)
@pytest.mark.parametrize(
    "idempotency",
    [IdempotencyClass.NOT_APPLICABLE, IdempotencyClass.NATURAL, IdempotencyClass.KEYED],
)
def test_pure_and_read_only_tools_accept_any_other_idempotency(
    side_effect: SideEffectClass, idempotency: IdempotencyClass
) -> None:
    metadata = _metadata(side_effect=side_effect, idempotency=idempotency)
    assert metadata.idempotency is idempotency


@pytest.mark.parametrize("side_effect", SIDE_EFFECTING_CLASSES)
@pytest.mark.parametrize("retry", RETRYABLE_CLASSES)
@pytest.mark.parametrize("idempotency", WEAK_IDEMPOTENCY_CLASSES)
def test_retryable_side_effecting_tools_reject_weak_idempotency(
    side_effect: SideEffectClass, retry: RetryClass, idempotency: IdempotencyClass
) -> None:
    with pytest.raises(ValueError, match="require natural or keyed idempotency"):
        _metadata(
            side_effect=side_effect,
            retry=retry,
            idempotency=idempotency,
            approval=_approval_for(side_effect),
        )


@pytest.mark.parametrize("side_effect", SIDE_EFFECTING_CLASSES)
@pytest.mark.parametrize("retry", RETRYABLE_CLASSES)
@pytest.mark.parametrize("idempotency", STRONG_IDEMPOTENCY_CLASSES)
def test_retryable_side_effecting_tools_accept_strong_idempotency(
    side_effect: SideEffectClass, retry: RetryClass, idempotency: IdempotencyClass
) -> None:
    metadata = _metadata(
        side_effect=side_effect,
        retry=retry,
        idempotency=idempotency,
        approval=_approval_for(side_effect),
    )
    assert metadata.retry is retry
    assert metadata.idempotency is idempotency


@pytest.mark.parametrize("side_effect", SIDE_EFFECTING_CLASSES)
@pytest.mark.parametrize("idempotency", [IdempotencyClass.NOT_APPLICABLE, IdempotencyClass.NONE])
def test_non_retryable_side_effecting_tools_accept_weak_idempotency(
    side_effect: SideEffectClass, idempotency: IdempotencyClass
) -> None:
    metadata = _metadata(
        side_effect=side_effect,
        retry=RetryClass.NEVER,
        idempotency=idempotency,
        approval=_approval_for(side_effect),
    )
    assert metadata.retry is RetryClass.NEVER


@pytest.mark.parametrize("approval", [ApprovalClass.NONE, ApprovalClass.POLICY_DEPENDENT])
def test_irreversible_tools_reject_missing_human_approval(approval: ApprovalClass) -> None:
    with pytest.raises(ValueError, match="irreversible tools require human approval"):
        _metadata(side_effect=SideEffectClass.IRREVERSIBLE, approval=approval)


def test_irreversible_tools_accept_required_human_approval() -> None:
    metadata = _metadata(
        side_effect=SideEffectClass.IRREVERSIBLE,
        approval=ApprovalClass.REQUIRED,
    )
    assert metadata.approval is ApprovalClass.REQUIRED


@pytest.mark.parametrize("approval", list(ApprovalClass))
def test_reversible_tools_accept_any_approval_class(approval: ApprovalClass) -> None:
    assert _metadata(approval=approval).approval is approval


def test_metadata_sensitivity_defaults_to_internal() -> None:
    assert _metadata().sensitivity is DataSensitivity.INTERNAL


@pytest.mark.parametrize("sensitivity", list(DataSensitivity))
def test_metadata_accepts_every_sensitivity_level(sensitivity: DataSensitivity) -> None:
    assert _metadata(sensitivity=sensitivity).sensitivity is sensitivity


def test_tool_metadata_is_frozen_and_hashable() -> None:
    metadata = _metadata()
    with pytest.raises(dataclasses.FrozenInstanceError):
        metadata.name = "renamed"  # type: ignore[misc]
    assert len({metadata, _metadata()}) == 1


def test_execution_request_accepts_valid_values() -> None:
    request = _request(attempt=2, timeout_seconds=1.5, idempotency_key="loopforge:r:a1")
    assert request.proposal == _proposal()
    assert request.attempt == 2
    assert request.timeout_seconds == 1.5
    assert request.idempotency_key == "loopforge:r:a1"


def test_execution_request_idempotency_key_defaults_to_none() -> None:
    assert _request().idempotency_key is None


@pytest.mark.parametrize("attempt", [0, -1, -100])
def test_execution_request_rejects_non_positive_attempt(attempt: int) -> None:
    with pytest.raises(ValueError, match="attempt must be positive"):
        _request(attempt=attempt)


@pytest.mark.parametrize("timeout_seconds", [0.0, -0.5])
def test_execution_request_rejects_non_positive_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        _request(timeout_seconds=timeout_seconds)


def test_execution_request_is_frozen() -> None:
    request = _request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.attempt = 2  # type: ignore[misc]


def test_tool_result_success_needs_no_failure_details() -> None:
    result = ToolResult(ok=True, observation="done")
    assert result.ok
    assert result.observation == "done"
    assert result.error_code is None
    assert result.failure_class is None


def test_tool_result_success_may_carry_an_error_code_annotation() -> None:
    result = ToolResult(ok=True, observation="done with warnings", error_code="WARN_DEPRECATED")
    assert result.ok
    assert result.error_code == "WARN_DEPRECATED"


@pytest.mark.parametrize("failure_class", list(ToolFailureClass))
def test_tool_result_rejects_success_with_failure_class(failure_class: ToolFailureClass) -> None:
    with pytest.raises(ValueError, match="successful tool result cannot declare failure_class"):
        ToolResult(ok=True, observation="done", failure_class=failure_class)


def test_tool_result_rejects_failure_without_failure_class() -> None:
    with pytest.raises(ValueError, match="failed tool result requires failure_class"):
        ToolResult(ok=False, observation="boom", error_code="E_TIMEOUT")


@pytest.mark.parametrize("failure_class", list(ToolFailureClass))
def test_tool_result_failure_accepts_each_failure_class(failure_class: ToolFailureClass) -> None:
    result = ToolResult(ok=False, observation="boom", failure_class=failure_class)
    assert not result.ok
    assert result.failure_class is failure_class


def test_tool_result_is_frozen() -> None:
    result = ToolResult(ok=True, observation="done")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.observation = "tampered"  # type: ignore[misc]


def test_tool_error_types_integrate_with_builtin_categories() -> None:
    assert issubclass(ToolContractError, TypeError)
    assert issubclass(UnknownToolError, LookupError)

    contract_msg = "adapter returned a malformed result"
    with pytest.raises(TypeError, match="malformed result"):
        raise ToolContractError(contract_msg)

    unknown_msg = "no tool named delete-everything"
    with pytest.raises(LookupError, match="no tool named"):
        raise UnknownToolError(unknown_msg)


def test_static_executor_satisfies_tool_executor_port() -> None:
    executor: ToolExecutorPort = _StaticExecutor()

    metadata = executor.metadata_for("inspect")
    assert metadata.name == "inspect"

    result = executor.execute(_request(idempotency_key="k1"))
    assert result.ok
    assert result.observation == "ran inspect"

    with pytest.raises(UnknownToolError, match="missing-tool"):
        executor.metadata_for("missing-tool")
