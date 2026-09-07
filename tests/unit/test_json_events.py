from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from loopforge.adapters.json_events import (
    _EVENT_TYPES,  # pyright: ignore[reportPrivateUsage]  # registry completeness check
    JsonEventCodec,
    UnknownEventTypeError,
    UnsupportedEventSchemaError,
    # Defensive decode helpers covered directly below.
    _construct_event,  # pyright: ignore[reportPrivateUsage]
    _to_jsonable,  # pyright: ignore[reportPrivateUsage]
)
from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import MAX_ARTIFACT_CONTENT_BYTES, ArtifactKind
from loopforge.domain.context import ContextAuthorityError, ContextItemSnapshot, ContextSource
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    ActionRejected,
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    ArtifactRecorded,
    BudgetDebited,
    CircuitOpened,
    ContextAssembled,
    Event,
    ModelTurnRecorded,
    OperatorInstruction,
    PlanCreated,
    ReflectionRecorded,
    RetryScheduled,
    RunStarted,
    RunStopped,
    ShadowDecisionRecorded,
    ToolExecutionStarted,
    ToolFailed,
    ToolSucceeded,
    VerificationFailed,
    VerificationPassed,
    WorkerMerged,
    WorkerSpawned,
    WorkerStopped,
)
from loopforge.domain.orchestration import MergeOutcome, WorkerOutcome
from loopforge.domain.policies import ShadowDecisionKind
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.security import TrustClass
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
    ContextItemId,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    StopReason,
    UsageDelta,
    WorkerId,
    WorkspaceId,
)

CODEC = JsonEventCodec()
NOW = datetime(2026, 8, 22, 12, 30, 15, tzinfo=UTC)
RUN = RunId("run-json")
_MISSING: Any = object()


def _proposal(**overrides: Any) -> ActionProposal:
    fields: dict[str, Any] = {
        "action_id": ActionId("a1"),
        "tool_name": "inspect",
        "arguments": {"path": "src"},
        "expected_observation": None,
    }
    fields.update(overrides)
    return ActionProposal(**fields)


def _metadata(**overrides: Any) -> ToolMetadata:
    fields: dict[str, Any] = {
        "name": "inspect",
        "risk": RiskLevel.READ_ONLY,
        "required_permission": Permission.READ,
        "side_effect": SideEffectClass.READ_ONLY,
        "retry": RetryClass.SAFE,
        "idempotency": IdempotencyClass.NATURAL,
        "approval": ApprovalClass.NONE,
        "timeout_seconds": 5.0,
        "sensitivity": DataSensitivity.INTERNAL,
    }
    fields.update(overrides)
    return ToolMetadata(**fields)


EXAMPLES: tuple[Event, ...] = (
    RunStarted(
        event_id=EventId("e01"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        objective="repair auth",
    ),
    PlanCreated(
        event_id=EventId("e02"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=2,
        caused_by=EventId("e01"),
        plan="inspect then patch",
    ),
    ActionProposed(
        event_id=EventId("e03"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=3,
        caused_by=EventId("e02"),
        proposal=_proposal(),
    ),
    ActionAuthorized(
        event_id=EventId("e04"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=4,
        caused_by=EventId("e03"),
        proposal=_proposal(),
        tool_metadata=_metadata(),
    ),
    ActionRejected(
        event_id=EventId("e05"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=5,
        caused_by=EventId("e03"),
        proposal=_proposal(expected_observation="all tests pass"),
        reason_code="POLICY_PERMISSION_DENIED",
    ),
    ToolExecutionStarted(
        event_id=EventId("e06"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=6,
        caused_by=EventId("e04"),
        action_id=ActionId("a1"),
        attempt=2,
        idempotency_key="loopforge:run-json:a1",
    ),
    ToolSucceeded(
        event_id=EventId("e07"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=7,
        caused_by=EventId("e06"),
        action_id=ActionId("a1"),
        observation="all tests pass",
        attempt=2,
    ),
    ToolFailed(
        event_id=EventId("e08"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=8,
        caused_by=EventId("e06"),
        action_id=ActionId("a1"),
        error_code="TOOL_TIMEOUT",
        error_message="timed out after 5s",
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=3,
    ),
    RetryScheduled(
        event_id=EventId("e09"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=9,
        caused_by=EventId("e08"),
        action_id=ActionId("a1"),
        next_attempt=3,
        delay_seconds=0.5,
        reason_code="RETRY_TRANSIENT_FAILURE",
    ),
    CircuitOpened(
        event_id=EventId("e10"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=10,
        caused_by=EventId("e08"),
        tool_name="inspect",
        reason_code="CIRCUIT_FAILURE_THRESHOLD",
    ),
    VerificationPassed(
        event_id=EventId("e11"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=11,
        summary="verification passed",
    ),
    VerificationFailed(
        event_id=EventId("e12"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=12,
        summary="tests still failing",
        score=0.25,
    ),
    ReflectionRecorded(
        event_id=EventId("e13"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=13,
        reflection="narrow the diff next attempt",
    ),
    BudgetDebited(
        event_id=EventId("e14"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=14,
        usage=UsageDelta(cost_usd=0.01, input_tokens=120, output_tokens=30, cached_input_tokens=10),
    ),
    ApprovalRequested(
        event_id=EventId("e15"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=15,
        caused_by=EventId("e04"),
        action_id=ActionId("a9"),
        reason="irreversible action",
    ),
    ApprovalGranted(
        event_id=EventId("e16"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=16,
        caused_by=EventId("e15"),
        action_id=ActionId("a9"),
    ),
    RunStopped(
        event_id=EventId("e17"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=17,
        reason=StopReason.SUCCESS_VERIFIED,
        summary="run completed",
    ),
    # Nullable-field variants beyond the one-per-type catalog above.
    ToolExecutionStarted(
        event_id=EventId("e18"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=18,
        action_id=ActionId("a2"),
        attempt=1,
        idempotency_key=None,
    ),
    VerificationFailed(
        event_id=EventId("e19"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=19,
        summary="score withheld",
        score=None,
    ),
    ContextAssembled(
        event_id=EventId("e20"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=20,
        context_items=(
            ContextItemSnapshot(
                item_id=ContextItemId("run-json:objective"),
                content="repair auth",
                trust=TrustClass.AUTHORIZED_HUMAN,
                source=ContextSource(
                    origin=TrustClass.AUTHORIZED_HUMAN,
                    reference="run:run-json:objective",
                    detail="operator-supplied run objective",
                ),
                sensitivity=DataSensitivity.INTERNAL,
                created_at=NOW,
            ),
        ),
    ),
    # Nullable-field variant exercising supersedes/expires_at serialization.
    ArtifactRecorded(
        event_id=EventId("e22"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=22,
        kind=ArtifactKind.WORKSPACE_SNAPSHOT,
        label="workspace:adder-regression",
        content="workspace_id=adder-regression\n\ndiff --git a/adder.py b/adder.py\n",
    ),
    ContextAssembled(
        event_id=EventId("e21"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=21,
        context_items=(
            ContextItemSnapshot(
                item_id=ContextItemId("run-json:plan"),
                content="inspect then patch",
                trust=TrustClass.RUNTIME_POLICY,
                source=ContextSource(
                    origin=TrustClass.RUNTIME_POLICY,
                    reference="run:run-json:plan",
                ),
                sensitivity=DataSensitivity.PUBLIC,
                created_at=NOW,
                supersedes=ContextItemId("run-json:objective"),
                expires_at=NOW + timedelta(hours=1),
            ),
        ),
    ),
    WorkerSpawned(
        event_id=EventId("e23"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=23,
        worker_id=WorkerId("worker-adder"),
        worker_run_id=RunId("worker-run-1"),
        workspace_id=WorkspaceId("ws-adder"),
        objective="repair adder.py",
        budget_share_cost_usd=0.5,
    ),
    WorkerStopped(
        event_id=EventId("e24"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=24,
        worker_id=WorkerId("worker-adder"),
        outcome=WorkerOutcome.BUDGET_EXHAUSTED,
        summary="share exhausted",
    ),
    WorkerMerged(
        event_id=EventId("e25"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=25,
        worker_id=WorkerId("worker-adder"),
        outcome=MergeOutcome.CONFLICT,
        revision=None,
        detail="merge aborted: overlapping edits",
    ),
    ApprovalRejected(
        event_id=EventId("e26"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=26,
        caused_by=EventId("e15"),
        action_id=ActionId("a9"),
        reason="operator denied the write",
    ),
    OperatorInstruction(
        event_id=EventId("e27"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=27,
        caused_by=EventId("e15"),
        instruction="focus on the failing test only",
        amends_objective=True,
    ),
    ModelTurnRecorded(
        event_id=EventId("e28"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=28,
        caused_by=EventId("e14"),
        provider="ollama",
        model="devstral-small-2:latest",
        action_id=ActionId("a1"),
    ),
    ShadowDecisionRecorded(
        event_id=EventId("e30"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=30,
        caused_by=EventId("e14"),
        policy_id="adaptive-context",
        policy_version=1,
        kind=ShadowDecisionKind.MODEL_ROUTE,
        decision="ollama/devstral-small-2:latest tier=economy",
        basis="reason_code=ROUTE_INITIAL_SELECTION",
    ),
    # Nullable-field variant exercising the PACS-015 lineage link.
    RunStarted(
        event_id=EventId("e29"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=29,
        objective="continue the repair",
        parent_run_id=RunId("parent-run-1"),
    ),
)

(
    RUN_STARTED,
    PLAN_CREATED,
    ACTION_PROPOSED,
    ACTION_AUTHORIZED,
    ACTION_REJECTED,
    TOOL_EXECUTION_STARTED,
    TOOL_SUCCEEDED,
    TOOL_FAILED,
    RETRY_SCHEDULED,
    CIRCUIT_OPENED,
    VERIFICATION_PASSED,
    VERIFICATION_FAILED,
    REFLECTION_RECORDED,
    BUDGET_DEBITED,
    APPROVAL_REQUESTED,
    APPROVAL_GRANTED,
    RUN_STOPPED,
    TOOL_EXECUTION_STARTED_UNKEYED,
    VERIFICATION_FAILED_UNSCORED,
    CONTEXT_ASSEMBLED,
    ARTIFACT_RECORDED,
    CONTEXT_ASSEMBLED_SUPERSEDING,
    WORKER_SPAWNED,
    WORKER_STOPPED,
    WORKER_MERGED,
    APPROVAL_REJECTED,
    OPERATOR_INSTRUCTION,
    MODEL_TURN_RECORDED,
    SHADOW_DECISION_RECORDED,
    RUN_STARTED_WITH_PARENT,
) = EXAMPLES


def _envelope(event: Event) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CODEC.encode(event)))


def _mutated_envelope(event: Event, key: str, value: Any) -> str:
    envelope = _envelope(event)
    if value is _MISSING:
        envelope.pop(key)
    else:
        envelope[key] = value
    return json.dumps(envelope)


def _mutated_body(event: Event, key: str, value: Any, *, section: str | None = None) -> str:
    envelope = _envelope(event)
    target = cast(dict[str, Any], envelope["event"])
    if section is not None:
        target = cast(dict[str, Any], target[section])
    if value is _MISSING:
        target.pop(key)
    else:
        target[key] = value
    return json.dumps(envelope)


def _legacy_tool_failed_payload(retryable: Any) -> str:
    envelope = _envelope(TOOL_FAILED)
    body = cast(dict[str, Any], envelope["event"])
    body.pop("failure_class")
    if retryable is _MISSING:
        body.pop("retryable", None)
    else:
        body["retryable"] = retryable
    return json.dumps(envelope)


def test_examples_cover_every_registered_event_type() -> None:
    assert {type(event).__name__ for event in EXAMPLES} == set(_EVENT_TYPES)
    assert len(_EVENT_TYPES) == 26


@pytest.mark.parametrize(
    "event", EXAMPLES, ids=lambda event: f"{type(event).__name__}-{event.sequence}"
)
def test_round_trip_preserves_every_event_type(event: Event) -> None:
    payload = CODEC.encode(event)

    decoded = CODEC.decode(payload)

    assert decoded == event
    # Encoding is canonical: decoding and re-encoding is a fixed point, and the
    # payload matches a sorted-key, compact-separator serialization.
    assert CODEC.encode(decoded) == payload
    assert payload == json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))


def test_encode_produces_byte_exact_canonical_envelope() -> None:
    assert CODEC.encode(RUN_STARTED) == (
        '{"event":{"caused_by":null,"event_id":"e01","objective":"repair auth",'
        '"occurred_at":"2026-08-22T12:30:15+00:00","parent_run_id":null,'
        '"run_id":"run-json","sequence":1},'
        '"event_type":"RunStarted","schema_version":1}'
    )


def test_encode_sorts_keys_at_every_level() -> None:
    envelope = json.loads(CODEC.encode(ACTION_AUTHORIZED), object_pairs_hook=dict)
    assert list(envelope) == sorted(envelope)
    body = envelope["event"]
    assert list(body) == sorted(body)
    assert list(body["proposal"]) == sorted(body["proposal"])
    assert list(body["tool_metadata"]) == sorted(body["tool_metadata"])


@pytest.mark.parametrize("payload", ["[]", "null", "42", '"just a string"', "true"])
def test_decode_rejects_non_object_envelope(payload: str) -> None:
    with pytest.raises(TypeError, match="event envelope must be a JSON object"):
        CODEC.decode(payload)


@pytest.mark.parametrize("version", [_MISSING, 0, 2, "1"])
def test_decode_rejects_missing_or_unsupported_schema_version(version: Any) -> None:
    payload = _mutated_envelope(RUN_STARTED, "schema_version", version)
    with pytest.raises(UnsupportedEventSchemaError, match="unsupported event schema version"):
        CODEC.decode(payload)


@pytest.mark.parametrize("event_type", [_MISSING, 42, "Ghost", "runstarted"])
def test_decode_rejects_missing_or_unknown_event_type(event_type: Any) -> None:
    payload = _mutated_envelope(RUN_STARTED, "event_type", event_type)
    with pytest.raises(UnknownEventTypeError, match="unknown event type"):
        CODEC.decode(payload)


@pytest.mark.parametrize("body", [_MISSING, None, [], "x", 1])
def test_decode_rejects_non_object_event_body(body: Any) -> None:
    payload = _mutated_envelope(RUN_STARTED, "event", body)
    with pytest.raises(TypeError, match="event must be a JSON object"):
        CODEC.decode(payload)


@pytest.mark.parametrize(
    ("event", "section", "key", "value", "match"),
    [
        (RUN_STARTED, None, "event_id", 7, "event_id must be a string"),
        (RUN_STARTED, None, "event_id", _MISSING, "event_id must be a string"),
        (RUN_STARTED, None, "run_id", None, "run_id must be a string"),
        (RUN_STARTED, None, "occurred_at", 123, "occurred_at must be a string"),
        (RUN_STARTED, None, "objective", 1, "objective must be a string"),
        (PLAN_CREATED, None, "plan", None, "plan must be a string"),
        (ACTION_REJECTED, None, "reason_code", ["denied"], "reason_code must be a string"),
        (TOOL_SUCCEEDED, None, "observation", 5, "observation must be a string"),
        (TOOL_FAILED, None, "error_code", None, "error_code must be a string"),
        (TOOL_FAILED, None, "error_message", {"detail": 1}, "error_message must be a string"),
        (RETRY_SCHEDULED, None, "reason_code", 9, "reason_code must be a string"),
        (CIRCUIT_OPENED, None, "tool_name", None, "tool_name must be a string"),
        (CIRCUIT_OPENED, None, "reason_code", 3, "reason_code must be a string"),
        (VERIFICATION_PASSED, None, "summary", 1, "summary must be a string"),
        (VERIFICATION_FAILED, None, "summary", None, "summary must be a string"),
        (REFLECTION_RECORDED, None, "reflection", [], "reflection must be a string"),
        (MODEL_TURN_RECORDED, None, "provider", 7, "provider must be a string"),
        (MODEL_TURN_RECORDED, None, "model", None, "model must be a string"),
        (MODEL_TURN_RECORDED, None, "action_id", [], "action_id must be a string"),
        (APPROVAL_REQUESTED, None, "action_id", 8, "action_id must be a string"),
        (APPROVAL_REQUESTED, None, "reason", None, "reason must be a string"),
        (APPROVAL_GRANTED, None, "action_id", None, "action_id must be a string"),
        (RUN_STOPPED, None, "summary", 7, "summary must be a string"),
        (RUN_STOPPED, None, "reason", None, "reason must be a string"),
        (ACTION_PROPOSED, "proposal", "action_id", None, "action_id must be a string"),
        (ACTION_PROPOSED, "proposal", "tool_name", 3, "tool_name must be a string"),
        (ACTION_AUTHORIZED, "tool_metadata", "name", 1, "name must be a string"),
        (ACTION_AUTHORIZED, "tool_metadata", "risk", 9, "risk must be a string"),
        (
            ACTION_AUTHORIZED,
            "tool_metadata",
            "required_permission",
            None,
            "required_permission must be a string",
        ),
        (ACTION_AUTHORIZED, "tool_metadata", "side_effect", 2, "side_effect must be a string"),
        (ACTION_AUTHORIZED, "tool_metadata", "retry", {}, "retry must be a string"),
        (ACTION_AUTHORIZED, "tool_metadata", "idempotency", [], "idempotency must be a string"),
        (ACTION_AUTHORIZED, "tool_metadata", "approval", 0, "approval must be a string"),
        (ACTION_AUTHORIZED, "tool_metadata", "sensitivity", 1.5, "sensitivity must be a string"),
    ],
)
def test_decode_rejects_mistyped_required_strings(
    event: Event, section: str | None, key: str, value: Any, match: str
) -> None:
    with pytest.raises(TypeError, match=match):
        CODEC.decode(_mutated_body(event, key, value, section=section))


def test_decode_run_started_decodes_a_missing_parent_run_id_as_none() -> None:
    # Pre-PACS-015 payloads simply lack the key; they must keep decoding.
    decoded = CODEC.decode(_mutated_body(RUN_STARTED, "parent_run_id", _MISSING))

    assert isinstance(decoded, RunStarted)
    assert decoded.parent_run_id is None


def test_decode_run_started_rejects_a_mistyped_parent_run_id() -> None:
    with pytest.raises(TypeError, match="parent_run_id must be a string or null"):
        CODEC.decode(_mutated_body(RUN_STARTED, "parent_run_id", 7))


def test_decode_model_turn_recorded_rejects_blank_identity() -> None:
    with pytest.raises(ValueError, match="model turn provider cannot be empty"):
        CODEC.decode(_mutated_body(MODEL_TURN_RECORDED, "provider", "   "))
    with pytest.raises(ValueError, match="model turn model cannot be empty"):
        CODEC.decode(_mutated_body(MODEL_TURN_RECORDED, "model", ""))


@pytest.mark.parametrize(
    ("event", "section", "key", "value", "match"),
    [
        (RUN_STARTED, None, "sequence", "1", "sequence must be an integer"),
        (RUN_STARTED, None, "sequence", True, "sequence must be an integer"),
        (TOOL_EXECUTION_STARTED, None, "attempt", "1", "attempt must be an integer"),
        (TOOL_EXECUTION_STARTED, None, "attempt", True, "attempt must be an integer"),
        (TOOL_SUCCEEDED, None, "attempt", 1.5, "attempt must be an integer"),
        (TOOL_SUCCEEDED, None, "attempt", False, "attempt must be an integer"),
        (TOOL_FAILED, None, "attempt", None, "attempt must be an integer"),
        (RETRY_SCHEDULED, None, "next_attempt", 2.5, "next_attempt must be an integer"),
        (RETRY_SCHEDULED, None, "next_attempt", _MISSING, "next_attempt must be an integer"),
        (BUDGET_DEBITED, "usage", "input_tokens", True, "input_tokens must be an integer"),
        (BUDGET_DEBITED, "usage", "output_tokens", "3", "output_tokens must be an integer"),
        (
            BUDGET_DEBITED,
            "usage",
            "cached_input_tokens",
            1.5,
            "cached_input_tokens must be an integer",
        ),
    ],
)
def test_decode_rejects_mistyped_integers(
    event: Event, section: str | None, key: str, value: Any, match: str
) -> None:
    with pytest.raises(TypeError, match=match):
        CODEC.decode(_mutated_body(event, key, value, section=section))


@pytest.mark.parametrize(
    ("event", "section", "key", "value", "match"),
    [
        (RETRY_SCHEDULED, None, "delay_seconds", "0.5", "delay_seconds must be numeric"),
        (RETRY_SCHEDULED, None, "delay_seconds", True, "delay_seconds must be numeric"),
        (BUDGET_DEBITED, "usage", "cost_usd", "0.01", "cost_usd must be numeric"),
        (BUDGET_DEBITED, "usage", "cost_usd", False, "cost_usd must be numeric"),
        (
            ACTION_AUTHORIZED,
            "tool_metadata",
            "timeout_seconds",
            "5",
            "timeout_seconds must be numeric",
        ),
        (
            ACTION_AUTHORIZED,
            "tool_metadata",
            "timeout_seconds",
            True,
            "timeout_seconds must be numeric",
        ),
    ],
)
def test_decode_rejects_mistyped_numbers(
    event: Event, section: str | None, key: str, value: Any, match: str
) -> None:
    with pytest.raises(TypeError, match=match):
        CODEC.decode(_mutated_body(event, key, value, section=section))


@pytest.mark.parametrize(
    ("event", "key", "value", "match"),
    [
        (ACTION_PROPOSED, "proposal", [], "proposal must be a JSON object"),
        (ACTION_PROPOSED, "proposal", _MISSING, "proposal must be a JSON object"),
        (ACTION_AUTHORIZED, "tool_metadata", "meta", "tool_metadata must be a JSON object"),
        (ACTION_AUTHORIZED, "tool_metadata", None, "tool_metadata must be a JSON object"),
        (BUDGET_DEBITED, "usage", 3, "usage must be a JSON object"),
    ],
)
def test_decode_rejects_non_object_nested_sections(
    event: Event, key: str, value: Any, match: str
) -> None:
    with pytest.raises(TypeError, match=match):
        CODEC.decode(_mutated_body(event, key, value))


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"path": 1},
        _MISSING,
    ],
)
def test_decode_rejects_invalid_proposal_arguments(value: Any) -> None:
    payload = _mutated_body(ACTION_PROPOSED, "arguments", value, section="proposal")
    with pytest.raises(TypeError, match="proposal arguments must be an object of string keys"):
        CODEC.decode(payload)


@pytest.mark.parametrize(
    ("event", "section", "key", "value", "match"),
    [
        (
            TOOL_EXECUTION_STARTED,
            None,
            "idempotency_key",
            5,
            "idempotency_key must be a string or null",
        ),
        (
            TOOL_EXECUTION_STARTED,
            None,
            "idempotency_key",
            True,
            "idempotency_key must be a string or null",
        ),
        (VERIFICATION_FAILED, None, "score", "high", "score must be numeric or null"),
        (VERIFICATION_FAILED, None, "score", [0.5], "score must be numeric or null"),
        (
            ACTION_PROPOSED,
            "proposal",
            "expected_observation",
            7,
            "expected_observation must be a string or null",
        ),
    ],
)
def test_decode_rejects_mistyped_optional_fields(
    event: Event, section: str | None, key: str, value: Any, match: str
) -> None:
    with pytest.raises(TypeError, match=match):
        CODEC.decode(_mutated_body(event, key, value, section=section))


def test_decode_accepts_null_and_string_idempotency_key() -> None:
    unkeyed = CODEC.decode(_mutated_body(TOOL_EXECUTION_STARTED, "idempotency_key", None))
    assert isinstance(unkeyed, ToolExecutionStarted)
    assert unkeyed.idempotency_key is None
    keyed = CODEC.decode(CODEC.encode(TOOL_EXECUTION_STARTED))
    assert isinstance(keyed, ToolExecutionStarted)
    assert keyed.idempotency_key == "loopforge:run-json:a1"


def test_decode_accepts_null_and_numeric_score() -> None:
    unscored = CODEC.decode(_mutated_body(VERIFICATION_FAILED, "score", None))
    assert isinstance(unscored, VerificationFailed)
    assert unscored.score is None
    decoded = CODEC.decode(_mutated_body(VERIFICATION_FAILED, "score", 1))
    assert isinstance(decoded, VerificationFailed)
    assert decoded.score == 1.0
    assert isinstance(decoded.score, float)


def test_decode_defaults_missing_inconclusive_to_false_and_round_trips_true() -> None:
    legacy = CODEC.decode(_mutated_body(VERIFICATION_FAILED, "inconclusive", _MISSING))
    assert isinstance(legacy, VerificationFailed)
    assert legacy.inconclusive is False
    flagged = CODEC.decode(_mutated_body(VERIFICATION_FAILED, "inconclusive", True))
    assert isinstance(flagged, VerificationFailed)
    assert flagged.inconclusive is True
    with pytest.raises(TypeError, match="inconclusive"):
        CODEC.decode(_mutated_body(VERIFICATION_FAILED, "inconclusive", "yes"))


def test_decode_accepts_null_and_string_expected_observation() -> None:
    decoded = CODEC.decode(
        _mutated_body(ACTION_REJECTED, "expected_observation", None, section="proposal")
    )
    assert isinstance(decoded, ActionRejected)
    assert decoded.proposal.expected_observation is None

    decoded_with = CODEC.decode(CODEC.encode(ACTION_REJECTED))
    assert isinstance(decoded_with, ActionRejected)
    assert decoded_with.proposal.expected_observation == "all tests pass"


def test_decode_decodes_missing_caused_by_as_none() -> None:
    decoded = CODEC.decode(_mutated_body(PLAN_CREATED, "caused_by", _MISSING))
    assert decoded.caused_by is None

    linked = CODEC.decode(CODEC.encode(PLAN_CREATED))
    assert linked.caused_by == EventId("e01")


@pytest.mark.parametrize(
    ("event", "section", "key", "value", "match"),
    [
        (RUN_STOPPED, None, "reason", "not-a-reason", "is not a valid StopReason"),
        (ACTION_AUTHORIZED, "tool_metadata", "risk", "bogus", "is not a valid RiskLevel"),
        (
            ACTION_AUTHORIZED,
            "tool_metadata",
            "sensitivity",
            "bogus",
            "is not a valid DataSensitivity",
        ),
        (TOOL_FAILED, None, "failure_class", "bogus", "is not a valid ToolFailureClass"),
    ],
)
def test_decode_rejects_unknown_enum_values(
    event: Event, section: str | None, key: str, value: Any, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        CODEC.decode(_mutated_body(event, key, value, section=section))


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("risk", "critical", "requires permission"),
        ("timeout_seconds", 0, "timeout_seconds must be positive"),
        ("name", "", "tool metadata name cannot be empty"),
    ],
)
def test_decode_enforces_tool_metadata_domain_invariants(key: str, value: Any, match: str) -> None:
    payload = _mutated_body(ACTION_AUTHORIZED, key, value, section="tool_metadata")
    with pytest.raises(ValueError, match=match):
        CODEC.decode(payload)


def test_decode_rejects_naive_occurred_at() -> None:
    payload = _mutated_body(RUN_STARTED, "occurred_at", "2026-08-22T12:30:15")
    with pytest.raises(ValueError, match="timezone-aware"):
        CODEC.decode(payload)


def test_decode_rejects_malformed_occurred_at() -> None:
    payload = _mutated_body(RUN_STARTED, "occurred_at", "not-a-date")
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        CODEC.decode(payload)


def test_occurred_at_round_trips_with_utc_and_microseconds() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    event = PlanCreated(
        event_id=EventId("e-tz"),
        run_id=RUN,
        occurred_at=moment,
        sequence=1,
        plan="utc",
    )
    decoded = CODEC.decode(CODEC.encode(event))
    assert decoded.occurred_at == moment
    assert decoded.occurred_at.utcoffset() == timedelta(0)


def test_occurred_at_round_trips_with_non_utc_offset() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=offset)
    event = PlanCreated(
        event_id=EventId("e-offset"),
        run_id=RUN,
        occurred_at=moment,
        sequence=1,
        plan="offset",
    )
    decoded = CODEC.decode(CODEC.encode(event))
    assert decoded.occurred_at == moment
    assert decoded.occurred_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_tool_failed_decodes_legacy_retryable_true_payload_as_transient() -> None:
    decoded = CODEC.decode(_legacy_tool_failed_payload(retryable=True))
    assert isinstance(decoded, ToolFailed)
    assert decoded.failure_class is ToolFailureClass.TRANSIENT
    assert decoded.action_id == ActionId("a1")
    assert decoded.error_code == "TOOL_TIMEOUT"


def test_tool_failed_decodes_legacy_retryable_false_payload_as_permanent() -> None:
    decoded = CODEC.decode(_legacy_tool_failed_payload(retryable=False))
    assert isinstance(decoded, ToolFailed)
    assert decoded.failure_class is ToolFailureClass.PERMANENT


@pytest.mark.parametrize("retryable", [_MISSING, "yes", 1])
def test_tool_failed_legacy_payload_requires_boolean_retryable(retryable: Any) -> None:
    with pytest.raises(TypeError, match="retryable must be a boolean"):
        CODEC.decode(_legacy_tool_failed_payload(retryable))


@pytest.mark.parametrize("failure_class", list(ToolFailureClass))
def test_tool_failed_round_trips_every_failure_class(failure_class: ToolFailureClass) -> None:
    event = ToolFailed(
        event_id=EventId("e-fc"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=1,
        action_id=ActionId("a1"),
        error_code="ERR",
        error_message="failure",
        failure_class=failure_class,
    )
    assert CODEC.decode(CODEC.encode(event)) == event


def test_tool_succeeded_attempt_defaults_to_one_when_absent() -> None:
    decoded = CODEC.decode(_mutated_body(TOOL_SUCCEEDED, "attempt", _MISSING))
    assert isinstance(decoded, ToolSucceeded)
    assert decoded.attempt == 1


def test_tool_failed_attempt_defaults_to_one_when_absent() -> None:
    decoded = CODEC.decode(_mutated_body(TOOL_FAILED, "attempt", _MISSING))
    assert isinstance(decoded, ToolFailed)
    assert decoded.attempt == 1


def test_construct_event_rejects_unregistered_type_defensively() -> None:
    base = {
        "event_id": "e1",
        "run_id": "r1",
        "occurred_at": NOW.isoformat(),
        "sequence": 1,
        "caused_by": None,
    }
    with pytest.raises(UnknownEventTypeError, match="Ghost"):
        _construct_event("Ghost", base)


def test_to_jsonable_lowers_sequences_and_mappings_recursively() -> None:
    assert _to_jsonable((NOW, 1, "x")) == [NOW.isoformat(), 1, "x"]
    assert _to_jsonable([NOW]) == [NOW.isoformat()]
    assert _to_jsonable({"key": (1,)}) == {"key": [1]}
    assert _to_jsonable(3.5) == 3.5


_METADATA_POOL: tuple[ToolMetadata, ...] = (
    _metadata(),
    _metadata(
        name="search",
        side_effect=SideEffectClass.PURE,
        idempotency=IdempotencyClass.NOT_APPLICABLE,
        sensitivity=DataSensitivity.PUBLIC,
    ),
    _metadata(
        name="patch",
        risk=RiskLevel.LOCAL_WRITE,
        required_permission=Permission.LOCAL_WRITE,
        side_effect=SideEffectClass.LOCAL_WRITE,
        idempotency=IdempotencyClass.KEYED,
        approval=ApprovalClass.POLICY_DEPENDENT,
        timeout_seconds=30.0,
        sensitivity=DataSensitivity.SENSITIVE,
    ),
    _metadata(
        name="notify",
        risk=RiskLevel.EXTERNAL_WRITE,
        required_permission=Permission.EXTERNAL_WRITE,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        retry=RetryClass.TRANSIENT_ONLY,
        timeout_seconds=10.0,
    ),
    _metadata(
        name="deploy",
        risk=RiskLevel.CRITICAL,
        required_permission=Permission.CRITICAL,
        side_effect=SideEffectClass.IRREVERSIBLE,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NONE,
        approval=ApprovalClass.REQUIRED,
        timeout_seconds=120.0,
        sensitivity=DataSensitivity.SECRET,
    ),
)

_TEXT = st.text(max_size=80)
_SAFE_TEXT = st.text(alphabet=st.characters(categories=("L", "N")), min_size=1, max_size=40)
_FINITE_FLOAT = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)
_NONNEGATIVE_FLOAT = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
_TOKEN_COUNT = st.integers(min_value=0, max_value=1_000_000)
_BASE_FIELDS: dict[str, Any] = {
    "event_id": st.text(max_size=40).map(EventId),
    "run_id": st.text(max_size=40).map(RunId),
    "occurred_at": st.datetimes(timezones=st.just(UTC)),
    "sequence": st.integers(min_value=1, max_value=1_000_000),
    "caused_by": st.one_of(st.none(), st.text(max_size=40).map(EventId)),
}
_PROPOSAL_STRATEGY = st.builds(
    ActionProposal,
    action_id=st.text(max_size=40).map(ActionId),
    tool_name=_SAFE_TEXT,
    arguments=st.dictionaries(st.text(max_size=20), st.text(max_size=40), max_size=5),
    expected_observation=st.one_of(st.none(), st.text(max_size=60)),
)
_USAGE_STRATEGY = st.builds(
    UsageDelta,
    cost_usd=_NONNEGATIVE_FLOAT,
    input_tokens=_TOKEN_COUNT,
    output_tokens=_TOKEN_COUNT,
    cached_input_tokens=_TOKEN_COUNT,
)
_ALL_EVENTS: st.SearchStrategy[Event] = st.one_of(
    [
        st.builds(RunStarted, **_BASE_FIELDS, objective=_TEXT),
        st.builds(PlanCreated, **_BASE_FIELDS, plan=_TEXT),
        st.builds(ActionProposed, **_BASE_FIELDS, proposal=_PROPOSAL_STRATEGY),
        st.builds(
            ActionAuthorized,
            **_BASE_FIELDS,
            proposal=_PROPOSAL_STRATEGY,
            tool_metadata=st.sampled_from(_METADATA_POOL),
        ),
        st.builds(
            ActionRejected, **_BASE_FIELDS, proposal=_PROPOSAL_STRATEGY, reason_code=_SAFE_TEXT
        ),
        st.builds(
            ToolExecutionStarted,
            **_BASE_FIELDS,
            action_id=st.text(max_size=40).map(ActionId),
            attempt=st.integers(min_value=1, max_value=100),
            idempotency_key=st.one_of(st.none(), _TEXT),
        ),
        st.builds(
            ToolSucceeded,
            **_BASE_FIELDS,
            action_id=st.text(max_size=40).map(ActionId),
            observation=_TEXT,
            attempt=st.integers(min_value=1, max_value=100),
        ),
        st.builds(
            ToolFailed,
            **_BASE_FIELDS,
            action_id=st.text(max_size=40).map(ActionId),
            error_code=_TEXT,
            error_message=_TEXT,
            failure_class=st.sampled_from(list(ToolFailureClass)),
            attempt=st.integers(min_value=1, max_value=100),
        ),
        st.builds(
            RetryScheduled,
            **_BASE_FIELDS,
            action_id=st.text(max_size=40).map(ActionId),
            next_attempt=st.integers(min_value=2, max_value=100),
            delay_seconds=_NONNEGATIVE_FLOAT,
            reason_code=_SAFE_TEXT,
        ),
        st.builds(CircuitOpened, **_BASE_FIELDS, tool_name=_SAFE_TEXT, reason_code=_SAFE_TEXT),
        st.builds(VerificationPassed, **_BASE_FIELDS, summary=_TEXT),
        st.builds(
            VerificationFailed,
            **_BASE_FIELDS,
            summary=_TEXT,
            score=st.one_of(st.none(), _FINITE_FLOAT),
        ),
        st.builds(ReflectionRecorded, **_BASE_FIELDS, reflection=_TEXT),
        st.builds(BudgetDebited, **_BASE_FIELDS, usage=_USAGE_STRATEGY),
        st.builds(
            ApprovalRequested,
            **_BASE_FIELDS,
            action_id=st.text(max_size=40).map(ActionId),
            reason=_TEXT,
        ),
        st.builds(ApprovalGranted, **_BASE_FIELDS, action_id=st.text(max_size=40).map(ActionId)),
        st.builds(
            RunStopped,
            **_BASE_FIELDS,
            reason=st.sampled_from(list(StopReason)),
            summary=_TEXT,
        ),
    ]
)


@given(_ALL_EVENTS)
@example(RUN_STARTED)
@example(TOOL_EXECUTION_STARTED_UNKEYED)
@example(VERIFICATION_FAILED_UNSCORED)
@settings(max_examples=75, derandomize=True)
def test_event_round_trip_is_a_canonical_fixed_point(event: Event) -> None:
    payload = CODEC.encode(event)
    decoded = CODEC.decode(payload)

    assert decoded == event
    assert CODEC.encode(decoded) == payload
    assert payload == json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))


# --- ArtifactRecorded decode hardening -------------------------------------------


def _artifact_envelope(**overrides: Any) -> str:
    envelope = _envelope(ARTIFACT_RECORDED)
    body = cast(dict[str, Any], envelope["event"])
    body.update(overrides)
    return json.dumps(envelope)


@pytest.mark.parametrize("key", ["kind", "label", "content"])
def test_decode_artifact_recorded_rejects_mistyped_required_strings(key: str) -> None:
    with pytest.raises(TypeError, match=f"{key} must be a string"):
        CODEC.decode(_artifact_envelope(**{key: 7}))


def test_decode_artifact_recorded_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="is not a valid ArtifactKind"):
        CODEC.decode(_artifact_envelope(kind="model_claimed_success"))


def test_decode_artifact_recorded_rejects_blank_label() -> None:
    with pytest.raises(ValueError, match="artifact label cannot be empty"):
        CODEC.decode(_artifact_envelope(label="  "))


def test_decode_artifact_recorded_rejects_control_characters_in_label() -> None:
    with pytest.raises(ValueError, match="control characters"):
        CODEC.decode(_artifact_envelope(label="workspace:forged\nline"))


def test_decode_artifact_recorded_rejects_content_beyond_the_byte_budget() -> None:
    oversized = "x" * (MAX_ARTIFACT_CONTENT_BYTES + 1)
    with pytest.raises(ValueError, match="byte budget"):
        CODEC.decode(_artifact_envelope(content=oversized))


# --- ContextAssembled decode hardening -------------------------------------------


def _mutated_context_item(key: str, value: Any, *, section: str | None = None) -> str:
    envelope = _envelope(CONTEXT_ASSEMBLED)
    body = cast(dict[str, Any], envelope["event"])
    items = cast(list[Any], body["context_items"])
    target = cast(dict[str, Any], items[0])
    if section is not None:
        target = cast(dict[str, Any], target[section])
    if value is _MISSING:
        target.pop(key)
    else:
        target[key] = value
    return json.dumps(envelope)


@pytest.mark.parametrize("value", ["nope", 42, {"item": 1}, True])
def test_decode_rejects_non_array_context_items(value: Any) -> None:
    payload = _mutated_body(CONTEXT_ASSEMBLED, "context_items", value)
    with pytest.raises(TypeError, match="context_items must be a JSON array"):
        CODEC.decode(payload)


@pytest.mark.parametrize("value", ["nope", 42, [1]])
def test_decode_rejects_non_object_context_item(value: Any) -> None:
    payload = _mutated_body(CONTEXT_ASSEMBLED, "context_items", [value])
    with pytest.raises(TypeError, match="context item snapshot must be a JSON object"):
        CODEC.decode(payload)


@pytest.mark.parametrize("key", ["item_id", "content", "trust", "sensitivity", "created_at"])
def test_decode_rejects_missing_required_context_item_fields(key: str) -> None:
    payload = _mutated_context_item(key, _MISSING)
    with pytest.raises(TypeError, match=f"{key} must be a string"):
        CODEC.decode(payload)


def test_decode_rejects_missing_context_item_source() -> None:
    payload = _mutated_context_item("source", _MISSING)
    with pytest.raises(TypeError, match="source must be a JSON object"):
        CODEC.decode(payload)


@pytest.mark.parametrize("value", [42, True, ["x"]])
def test_decode_rejects_mistyped_context_item_supersedes(value: Any) -> None:
    payload = _mutated_context_item("supersedes", value)
    with pytest.raises(TypeError, match="supersedes must be a string or null"):
        CODEC.decode(payload)


@pytest.mark.parametrize("value", [42, False, {"at": "now"}])
def test_decode_rejects_mistyped_context_item_expires_at(value: Any) -> None:
    payload = _mutated_context_item("expires_at", value)
    with pytest.raises(TypeError, match="expires_at must be a string or null"):
        CODEC.decode(payload)


@pytest.mark.parametrize("value", [42, True, ["detail"]])
def test_decode_rejects_mistyped_context_source_detail(value: Any) -> None:
    payload = _mutated_context_item("detail", value, section="source")
    with pytest.raises(TypeError, match="source detail must be a string or null"):
        CODEC.decode(payload)


@pytest.mark.parametrize("key", ["origin", "reference"])
def test_decode_rejects_mistyped_context_source_fields(key: str) -> None:
    payload = _mutated_context_item(key, 42, section="source")
    with pytest.raises(TypeError, match=f"{key} must be a string"):
        CODEC.decode(payload)


@pytest.mark.parametrize("value", ["god_mode", "RUNTIME_POLICY", ""])
def test_decode_rejects_unknown_context_trust_values(value: str) -> None:
    payload = _mutated_context_item("trust", value)
    with pytest.raises(ValueError, match="is not a valid TrustClass"):
        CODEC.decode(payload)


def test_decode_rejects_unknown_context_sensitivity() -> None:
    payload = _mutated_context_item("sensitivity", "cosmic")
    with pytest.raises(ValueError, match="is not a valid DataSensitivity"):
        CODEC.decode(payload)


def test_decode_rejects_secret_context_items() -> None:
    payload = _mutated_context_item("sensitivity", "secret")
    with pytest.raises(ContextAuthorityError, match="must never be persisted"):
        CODEC.decode(payload)


@pytest.mark.parametrize("key", ["created_at", "expires_at"])
def test_decode_rejects_naive_context_item_datetimes(key: str) -> None:
    payload = _mutated_context_item(key, "2026-08-22T12:30:15")
    with pytest.raises(ValueError, match=f"{key} must be timezone-aware"):
        CODEC.decode(payload)


@pytest.mark.parametrize("key", ["created_at", "expires_at"])
def test_decode_rejects_malformed_context_item_datetimes(key: str) -> None:
    payload = _mutated_context_item(key, "not-a-date")
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        CODEC.decode(payload)


def test_decode_rejects_context_trust_origin_mismatch() -> None:
    payload = _mutated_context_item("origin", "untrusted_content", section="source")
    with pytest.raises(ValueError, match="trust must match the origin"):
        CODEC.decode(payload)


def test_decode_rejects_context_item_expiry_before_creation() -> None:
    payload = _mutated_context_item("expires_at", "2020-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="expires_at must be after created_at"):
        CODEC.decode(payload)


def test_decode_accepts_null_supersedes_and_expires_at() -> None:
    decoded = CODEC.decode(CODEC.encode(CONTEXT_ASSEMBLED))

    assert isinstance(decoded, ContextAssembled)
    assert decoded.context_items[0].supersedes is None
    assert decoded.context_items[0].expires_at is None


def test_decode_preserves_supersedes_and_expires_at() -> None:
    decoded = CODEC.decode(CODEC.encode(CONTEXT_ASSEMBLED_SUPERSEDING))

    assert isinstance(decoded, ContextAssembled)
    item = decoded.context_items[0]
    assert item.supersedes == ContextItemId("run-json:objective")
    assert item.expires_at == NOW + timedelta(hours=1)


# --- ContextAssembled prompt template metadata ---------------------------------


def test_context_assembled_round_trip_with_prompt_template_metadata() -> None:
    event = ContextAssembled(
        event_id=EventId("e30"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=30,
        context_items=CONTEXT_ASSEMBLED.context_items,
        prompt_template_id="loopforge.controller",
        prompt_template_version="1.0.0",
    )
    decoded = CODEC.decode(CODEC.encode(event))
    assert decoded == event


def test_context_assembled_legacy_payload_without_template_metadata_decodes() -> None:
    payload = _mutated_body(CONTEXT_ASSEMBLED, "prompt_template_id", _MISSING)
    envelope = json.loads(payload)
    body = cast(dict[str, Any], envelope["event"])
    body.pop("prompt_template_version")
    decoded = CODEC.decode(json.dumps(envelope))
    assert isinstance(decoded, ContextAssembled)
    assert decoded.prompt_template_id is None
    assert decoded.prompt_template_version is None


@pytest.mark.parametrize("value", [42, True, {"id": 1}])
def test_decode_rejects_non_string_prompt_template_id(value: Any) -> None:
    payload = _mutated_body(CONTEXT_ASSEMBLED, "prompt_template_id", value)
    with pytest.raises(TypeError, match="prompt_template_id must be a string or null"):
        CODEC.decode(payload)


@pytest.mark.parametrize("value", [42, True, ["1.0.0"]])
def test_decode_rejects_non_string_prompt_template_version(value: Any) -> None:
    payload = _mutated_body(CONTEXT_ASSEMBLED, "prompt_template_version", value)
    with pytest.raises(TypeError, match="prompt_template_version must be a string or null"):
        CODEC.decode(payload)


def test_decode_rejects_template_id_without_version() -> None:
    payload = _mutated_body(CONTEXT_ASSEMBLED, "prompt_template_id", "loopforge.controller")
    with pytest.raises(ValueError, match="must be recorded together"):
        CODEC.decode(payload)


def test_event_rejects_template_version_without_id() -> None:
    with pytest.raises(ValueError, match="must be recorded together"):
        ContextAssembled(
            event_id=EventId("e31"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=31,
            context_items=(),
            prompt_template_version="1.0.0",
        )


def test_event_rejects_empty_template_id_or_version() -> None:
    with pytest.raises(ValueError, match="prompt template id cannot be empty"):
        ContextAssembled(
            event_id=EventId("e32"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=32,
            context_items=(),
            prompt_template_id=" ",
            prompt_template_version="1.0.0",
        )
    with pytest.raises(ValueError, match="prompt template version cannot be empty"):
        ContextAssembled(
            event_id=EventId("e33"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=33,
            context_items=(),
            prompt_template_id="loopforge.controller",
            prompt_template_version="",
        )
