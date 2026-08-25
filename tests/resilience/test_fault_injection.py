from __future__ import annotations

from collections import deque
from typing import cast

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from loopforge.adapters.faults import (
    FaultInjectingTools,
    InjectedProcessCrash,
    ToolFault,
    ToolFaultKind,
)
from loopforge.domain.actions import ActionProposal
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, Permission, RiskLevel
from loopforge.ports.tools import (
    ToolExecutionRequest,
    ToolExecutorPort,
    ToolResult,
    UnknownToolError,
)


def _metadata(name: str = "inspect") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _request(tool_name: str = "inspect", *, attempt: int = 1) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        proposal=ActionProposal(ActionId("a1"), tool_name, {}),
        attempt=attempt,
        timeout_seconds=5.0,
    )


def _ok(observation: str = "done") -> ToolResult:
    return ToolResult(ok=True, observation=observation)


class RecordingTools(ToolExecutorPort):
    """Deterministic delegate that records requests and replays scripted results."""

    def __init__(self, results: list[ToolResult], *, metadata: list[ToolMetadata]) -> None:
        self._results = deque(results)
        self._metadata = {item.name: item for item in metadata}
        self.requests: list[ToolExecutionRequest] = []

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        try:
            return self._metadata[tool_name]
        except KeyError as exc:
            msg = f"unknown tool: {tool_name}"
            raise UnknownToolError(msg) from exc

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        self.requests.append(request)
        if not self._results:
            msg = "recording tool results exhausted"
            raise RuntimeError(msg)
        return self._results.popleft()


def test_metadata_for_delegates_to_wrapped_tool() -> None:
    tools = FaultInjectingTools(RecordingTools([], metadata=[_metadata()]), faults={})

    assert tools.metadata_for("inspect") == _metadata()


def test_metadata_for_propagates_unknown_tool_error() -> None:
    tools = FaultInjectingTools(RecordingTools([], metadata=[_metadata()]), faults={})

    with pytest.raises(UnknownToolError, match="unknown tool: missing"):
        tools.metadata_for("missing")


def test_execute_without_faults_passes_request_through_and_counts_invocation() -> None:
    delegate = RecordingTools([_ok("all tests pass")], metadata=[_metadata()])
    tools = FaultInjectingTools(delegate, faults={})
    request = _request(attempt=2)

    result = tools.execute(request)

    assert result == _ok("all tests pass")
    assert delegate.requests == [request]
    assert tools.invocations == {"inspect": 1}


def test_transient_timeout_fails_without_invoking_delegate() -> None:
    # No scripted results: any delegate execution raises instead of returning.
    delegate = RecordingTools([], metadata=[_metadata()])
    tools = FaultInjectingTools(
        delegate,
        faults={"inspect": [ToolFault(kind=ToolFaultKind.TRANSIENT_TIMEOUT)]},
    )

    result = tools.execute(_request())

    assert result == ToolResult(
        ok=False,
        observation="injected timeout",
        error_code="TIMEOUT",
        failure_class=ToolFailureClass.TRANSIENT,
    )
    assert delegate.requests == []
    assert tools.invocations == {"inspect": 1}


def test_transient_timeout_uses_custom_message_and_error_code() -> None:
    delegate = RecordingTools([], metadata=[_metadata()])
    tools = FaultInjectingTools(
        delegate,
        faults={
            "inspect": [
                ToolFault(
                    kind=ToolFaultKind.TRANSIENT_TIMEOUT,
                    error_code="GATEWAY_TIMEOUT",
                    message="upstream did not answer in 2s",
                )
            ]
        },
    )

    result = tools.execute(_request())

    assert result == ToolResult(
        ok=False,
        observation="upstream did not answer in 2s",
        error_code="GATEWAY_TIMEOUT",
        failure_class=ToolFailureClass.TRANSIENT,
    )
    assert delegate.requests == []


def test_ambiguous_after_success_reports_failure_after_delegate_side_effect() -> None:
    delegate = RecordingTools([_ok("remote write applied")], metadata=[_metadata()])
    tools = FaultInjectingTools(
        delegate,
        faults={"inspect": [ToolFault(kind=ToolFaultKind.AMBIGUOUS_AFTER_SUCCESS)]},
    )

    result = tools.execute(_request())

    # The side effect happened (delegate ran once) but the observation is lost.
    assert len(delegate.requests) == 1
    assert result == ToolResult(
        ok=False,
        observation="response lost after remote success",
        error_code="AMBIGUOUS_RESPONSE_LOSS",
        failure_class=ToolFailureClass.AMBIGUOUS_OUTCOME,
    )


def test_ambiguous_after_success_uses_custom_message_and_error_code() -> None:
    delegate = RecordingTools([_ok()], metadata=[_metadata()])
    tools = FaultInjectingTools(
        delegate,
        faults={
            "inspect": [
                ToolFault(
                    kind=ToolFaultKind.AMBIGUOUS_AFTER_SUCCESS,
                    error_code="HTTP_EOF_AFTER_COMMIT",
                    message="connection dropped after commit",
                )
            ]
        },
    )

    result = tools.execute(_request())

    assert result == ToolResult(
        ok=False,
        observation="connection dropped after commit",
        error_code="HTTP_EOF_AFTER_COMMIT",
        failure_class=ToolFailureClass.AMBIGUOUS_OUTCOME,
    )


def test_ambiguous_fault_returns_delegate_failure_unchanged() -> None:
    failure = ToolResult(
        ok=False,
        observation="validation rejected",
        error_code="INVALID_INPUT",
        failure_class=ToolFailureClass.PERMANENT,
    )
    delegate = RecordingTools([failure], metadata=[_metadata()])
    tools = FaultInjectingTools(
        delegate,
        faults={"inspect": [ToolFault(kind=ToolFaultKind.AMBIGUOUS_AFTER_SUCCESS)]},
    )

    result = tools.execute(_request())

    # A genuine failure is not dressed up as an ambiguous one.
    assert result == failure
    assert len(delegate.requests) == 1


def test_crash_after_success_raises_after_delegate_side_effect() -> None:
    delegate = RecordingTools([_ok("file written")], metadata=[_metadata()])
    tools = FaultInjectingTools(
        delegate,
        faults={"inspect": [ToolFault(kind=ToolFaultKind.CRASH_AFTER_SUCCESS)]},
    )

    with pytest.raises(InjectedProcessCrash, match="injected crash after side effect"):
        tools.execute(_request())

    assert len(delegate.requests) == 1
    assert tools.invocations == {"inspect": 1}


def test_crash_after_success_uses_custom_message() -> None:
    delegate = RecordingTools([_ok()], metadata=[_metadata()])
    tools = FaultInjectingTools(
        delegate,
        faults={
            "inspect": [
                ToolFault(kind=ToolFaultKind.CRASH_AFTER_SUCCESS, message="SIGKILL after write")
            ]
        },
    )

    with pytest.raises(InjectedProcessCrash, match="SIGKILL after write"):
        tools.execute(_request())


def test_crash_fault_does_not_raise_when_delegate_fails() -> None:
    failure = ToolResult(
        ok=False,
        observation="disk full",
        error_code="ENOSPC",
        failure_class=ToolFailureClass.PERMANENT,
    )
    delegate = RecordingTools([failure], metadata=[_metadata()])
    tools = FaultInjectingTools(
        delegate,
        faults={"inspect": [ToolFault(kind=ToolFaultKind.CRASH_AFTER_SUCCESS)]},
    )

    assert tools.execute(_request()) == failure
    assert len(delegate.requests) == 1


def test_injected_process_crash_is_a_runtime_error() -> None:
    assert issubclass(InjectedProcessCrash, RuntimeError)


def test_fault_scripts_are_per_tool_and_consumed_fifo() -> None:
    delegate = RecordingTools(
        [_ok("one"), _ok("two"), _ok("three")],
        metadata=[_metadata(), _metadata("other")],
    )
    tools = FaultInjectingTools(
        delegate,
        faults={
            "inspect": [
                ToolFault(kind=ToolFaultKind.TRANSIENT_TIMEOUT),
                ToolFault(kind=ToolFaultKind.AMBIGUOUS_AFTER_SUCCESS),
            ],
            "other": [ToolFault(kind=ToolFaultKind.TRANSIENT_TIMEOUT)],
        },
    )

    first = tools.execute(_request("inspect"))
    second = tools.execute(_request("inspect"))
    third = tools.execute(_request("inspect"))  # script exhausted: pass-through
    other = tools.execute(_request("other"))

    assert first.error_code == "TIMEOUT"
    assert second.error_code == "AMBIGUOUS_RESPONSE_LOSS"
    assert third == _ok("two")  # transient timeout consumed no delegate result
    assert other.error_code == "TIMEOUT"
    assert tools.invocations == {"inspect": 3, "other": 1}
    assert [request.proposal.tool_name for request in delegate.requests] == [
        "inspect",
        "inspect",
    ]


def test_unsupported_fault_kind_fails_loudly() -> None:
    bogus = cast(ToolFaultKind, "unsupported_fault")
    delegate = RecordingTools([_ok()], metadata=[_metadata()])
    tools = FaultInjectingTools(delegate, faults={"inspect": [ToolFault(kind=bogus)]})

    with pytest.raises(AssertionError, match="unsupported fault"):
        tools.execute(_request())


@given(kind=st.sampled_from(ToolFaultKind))
def test_tool_fault_kind_round_trips_through_str(kind: ToolFaultKind) -> None:
    assert ToolFaultKind(str(kind)) is kind
    assert str(kind) == kind.value


_OUTCOMES = st.lists(st.tuples(st.booleans(), st.text()), min_size=1, max_size=10)


@given(outcomes=_OUTCOMES)
@example(outcomes=[(True, "all tests pass")])
@example(outcomes=[(False, "boom")])
def test_pass_through_matches_delegate_for_arbitrary_results(
    outcomes: list[tuple[bool, str]],
) -> None:
    scripted = [
        _ok(observation)
        if ok
        else ToolResult(
            ok=False,
            observation=observation,
            error_code="E",
            failure_class=ToolFailureClass.PERMANENT,
        )
        for ok, observation in outcomes
    ]
    delegate = RecordingTools(list(scripted), metadata=[_metadata()])
    tools = FaultInjectingTools(delegate, faults={"inspect": []})

    returned = [tools.execute(_request()) for _ in scripted]

    assert returned == scripted
    assert len(delegate.requests) == len(scripted)
    assert tools.invocations["inspect"] == len(scripted)


_NON_CRASH_FAULTS = st.lists(
    st.sampled_from([ToolFaultKind.TRANSIENT_TIMEOUT, ToolFaultKind.AMBIGUOUS_AFTER_SUCCESS]),
    max_size=8,
)


@given(fault_kinds=_NON_CRASH_FAULTS, extra_calls=st.integers(min_value=0, max_value=4))
@example(fault_kinds=[ToolFaultKind.TRANSIENT_TIMEOUT], extra_calls=1)
@example(fault_kinds=[], extra_calls=0)
def test_fault_script_accounting_invariant(
    fault_kinds: list[ToolFaultKind], extra_calls: int
) -> None:
    # Invariant: transient timeouts never reach the delegate, ambiguous faults do,
    # and every call is counted exactly once regardless of the injected fault mix.
    expected_delegate_calls = (
        sum(1 for kind in fault_kinds if kind is not ToolFaultKind.TRANSIENT_TIMEOUT) + extra_calls
    )
    delegate = RecordingTools([_ok()] * expected_delegate_calls, metadata=[_metadata()])
    tools = FaultInjectingTools(
        delegate,
        faults={"inspect": [ToolFault(kind=kind) for kind in fault_kinds]},
    )

    results = [tools.execute(_request()) for _ in range(len(fault_kinds) + extra_calls)]

    assert tools.invocations["inspect"] == len(fault_kinds) + extra_calls
    assert len(delegate.requests) == expected_delegate_calls
    expected_failure_class = {
        ToolFaultKind.TRANSIENT_TIMEOUT: ToolFailureClass.TRANSIENT,
        ToolFaultKind.AMBIGUOUS_AFTER_SUCCESS: ToolFailureClass.AMBIGUOUS_OUTCOME,
    }
    for kind, result in zip(fault_kinds, results[: len(fault_kinds)], strict=True):
        assert not result.ok
        assert result.failure_class is expected_failure_class[kind]
    assert all(result.ok for result in results[len(fault_kinds) :])
