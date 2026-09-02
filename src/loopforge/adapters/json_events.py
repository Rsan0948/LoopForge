from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any, Final, cast

from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import ArtifactKind
from loopforge.domain.context import ContextItemSnapshot, ContextSource
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
    DomainEvent,
    Event,
    OperatorInstruction,
    PlanCreated,
    ReflectionRecorded,
    RetryScheduled,
    RunStarted,
    RunStopped,
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

SCHEMA_VERSION: Final[int] = 1

_EVENT_TYPES: Final[dict[str, type[DomainEvent]]] = {
    cls.__name__: cls
    for cls in (
        RunStarted,
        PlanCreated,
        ActionProposed,
        ActionAuthorized,
        ActionRejected,
        ToolExecutionStarted,
        ToolSucceeded,
        ToolFailed,
        RetryScheduled,
        CircuitOpened,
        VerificationPassed,
        VerificationFailed,
        ReflectionRecorded,
        ContextAssembled,
        ArtifactRecorded,
        BudgetDebited,
        ApprovalRequested,
        ApprovalGranted,
        ApprovalRejected,
        OperatorInstruction,
        RunStopped,
        WorkerSpawned,
        WorkerStopped,
        WorkerMerged,
    )
}


class UnsupportedEventSchemaError(ValueError):
    """Raised when a serialized event uses an unsupported schema version."""


class UnknownEventTypeError(ValueError):
    """Raised when the event envelope names an unregistered event type."""


class JsonEventCodec:
    """Versioned JSON codec for the current domain event catalog.

    The durable SQLite store in the next milestone can depend on this port/adapter
    without embedding serialization logic into persistence itself.
    """

    def encode(self, event: Event) -> str:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "event_type": type(event).__name__,
            "event": _to_jsonable(asdict(event)),
        }
        return json.dumps(envelope, sort_keys=True, separators=(",", ":"))

    def decode(self, payload: str) -> Event:
        parsed: Any = json.loads(payload)
        if not isinstance(parsed, dict):
            msg = "event envelope must be a JSON object"
            raise TypeError(msg)
        raw = cast(dict[str, Any], parsed)

        version = raw.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
            msg = f"unsupported event schema version: {version!r}"
            raise UnsupportedEventSchemaError(msg)

        event_type = raw.get("event_type")
        if not isinstance(event_type, str) or event_type not in _EVENT_TYPES:
            msg = f"unknown event type: {event_type!r}"
            raise UnknownEventTypeError(msg)

        event_data = _required_object(raw, "event")
        return _construct_event(event_type, event_data)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        mapping = cast(dict[Any, Any], value)
        return {str(key): _to_jsonable(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        sequence = cast(list[Any] | tuple[Any, ...], value)
        return [_to_jsonable(item) for item in sequence]
    # StrEnum and NewType-backed strings serialize naturally. Dataclasses have
    # already been lowered by asdict().
    return value


def _base(data: dict[str, Any]) -> dict[str, Any]:
    caused_by_raw = data.get("caused_by")
    if caused_by_raw is not None and not isinstance(caused_by_raw, str):
        msg = "caused_by must be a string or null"
        raise TypeError(msg)
    return {
        "event_id": EventId(_required_str(data, "event_id")),
        "run_id": RunId(_required_str(data, "run_id")),
        "occurred_at": datetime.fromisoformat(_required_str(data, "occurred_at")),
        "sequence": _required_int(data, "sequence"),
        "caused_by": EventId(caused_by_raw) if isinstance(caused_by_raw, str) else None,
    }


def _construct_run_started(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return RunStarted(**base, objective=_required_str(data, "objective"))


def _construct_plan_created(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return PlanCreated(**base, plan=_required_str(data, "plan"))


def _required_object(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        msg = f"{key} must be a JSON object"
        raise TypeError(msg)
    return cast(dict[str, Any], value)


def _construct_action_proposed(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return ActionProposed(**base, proposal=_proposal(_required_object(data, "proposal")))


def _construct_action_authorized(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return ActionAuthorized(
        **base,
        proposal=_proposal(_required_object(data, "proposal")),
        tool_metadata=_tool_metadata(_required_object(data, "tool_metadata")),
    )


def _construct_action_rejected(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return ActionRejected(
        **base,
        proposal=_proposal(_required_object(data, "proposal")),
        reason_code=_required_str(data, "reason_code"),
    )


def _construct_tool_execution_started(base: dict[str, Any], data: dict[str, Any]) -> Event:
    key = data.get("idempotency_key")
    if key is not None and not isinstance(key, str):
        msg_2 = "idempotency_key must be a string or null"
        raise TypeError(msg_2)
    return ToolExecutionStarted(
        **base,
        action_id=ActionId(_required_str(data, "action_id")),
        attempt=_required_int(data, "attempt"),
        idempotency_key=key,
    )


def _construct_tool_succeeded(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return ToolSucceeded(
        **base,
        action_id=ActionId(_required_str(data, "action_id")),
        observation=_required_str(data, "observation"),
        attempt=_optional_int(data, "attempt", default=1),
    )


def _construct_tool_failed(base: dict[str, Any], data: dict[str, Any]) -> Event:
    failure_raw = data.get("failure_class")
    if failure_raw is not None and not isinstance(failure_raw, str):
        msg = "failure_class must be a string or null"
        raise TypeError(msg)
    if isinstance(failure_raw, str):
        failure_class = ToolFailureClass(failure_raw)
    else:
        # Backward-compatible decode for PACS-001/002 schema-v1 payloads.
        legacy_retryable = _required_bool(data, "retryable")
        failure_class = (
            ToolFailureClass.TRANSIENT if legacy_retryable else ToolFailureClass.PERMANENT
        )
    return ToolFailed(
        **base,
        action_id=ActionId(_required_str(data, "action_id")),
        error_code=_required_str(data, "error_code"),
        error_message=_required_str(data, "error_message"),
        failure_class=failure_class,
        attempt=_optional_int(data, "attempt", default=1),
    )


def _construct_retry_scheduled(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return RetryScheduled(
        **base,
        action_id=ActionId(_required_str(data, "action_id")),
        next_attempt=_required_int(data, "next_attempt"),
        delay_seconds=_required_number(data, "delay_seconds"),
        reason_code=_required_str(data, "reason_code"),
    )


def _construct_circuit_opened(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return CircuitOpened(
        **base,
        tool_name=_required_str(data, "tool_name"),
        reason_code=_required_str(data, "reason_code"),
    )


def _construct_verification_passed(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return VerificationPassed(**base, summary=_required_str(data, "summary"))


def _construct_verification_failed(base: dict[str, Any], data: dict[str, Any]) -> Event:
    score = data.get("score")
    if score is not None and (not isinstance(score, (int, float)) or isinstance(score, bool)):
        msg = "score must be numeric or null"
        raise TypeError(msg)
    if score is not None and not math.isfinite(float(score)):
        msg = "score must be finite"
        raise ValueError(msg)
    return VerificationFailed(
        **base,
        summary=_required_str(data, "summary"),
        score=float(score) if score is not None else None,
    )


def _construct_reflection_recorded(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return ReflectionRecorded(**base, reflection=_required_str(data, "reflection"))


def _construct_context_assembled(base: dict[str, Any], data: dict[str, Any]) -> Event:
    items_raw: Any = data.get("context_items")
    if not isinstance(items_raw, list):
        msg = "context_items must be a JSON array"
        raise TypeError(msg)
    items = cast(list[Any], items_raw)
    return ContextAssembled(
        **base,
        context_items=tuple(_context_item_snapshot(item) for item in items),
        prompt_template_id=_optional_str(data, "prompt_template_id"),
        prompt_template_version=_optional_str(data, "prompt_template_version"),
    )


def _context_item_snapshot(data: Any) -> ContextItemSnapshot:
    if not isinstance(data, dict):
        msg = "context item snapshot must be a JSON object"
        raise TypeError(msg)
    item = cast(dict[str, Any], data)
    supersedes_raw = item.get("supersedes")
    if supersedes_raw is not None and not isinstance(supersedes_raw, str):
        msg_2 = "supersedes must be a string or null"
        raise TypeError(msg_2)
    expires_raw = item.get("expires_at")
    if expires_raw is not None and not isinstance(expires_raw, str):
        msg_3 = "expires_at must be a string or null"
        raise TypeError(msg_3)
    return ContextItemSnapshot(
        item_id=ContextItemId(_required_str(item, "item_id")),
        content=_required_str(item, "content"),
        trust=TrustClass(_required_str(item, "trust")),
        source=_context_source(_required_object(item, "source")),
        sensitivity=DataSensitivity(_required_str(item, "sensitivity")),
        created_at=datetime.fromisoformat(_required_str(item, "created_at")),
        supersedes=ContextItemId(supersedes_raw) if isinstance(supersedes_raw, str) else None,
        expires_at=datetime.fromisoformat(expires_raw) if isinstance(expires_raw, str) else None,
    )


def _context_source(data: dict[str, Any]) -> ContextSource:
    detail = data.get("detail")
    if detail is not None and not isinstance(detail, str):
        msg = "source detail must be a string or null"
        raise TypeError(msg)
    return ContextSource(
        origin=TrustClass(_required_str(data, "origin")),
        reference=_required_str(data, "reference"),
        detail=detail if isinstance(detail, str) else "",
    )


def _construct_artifact_recorded(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return ArtifactRecorded(
        **base,
        kind=ArtifactKind(_required_str(data, "kind")),
        label=_required_str(data, "label"),
        content=_required_str(data, "content"),
    )


def _construct_budget_debited(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return BudgetDebited(**base, usage=_usage(_required_object(data, "usage")))


def _construct_approval_requested(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return ApprovalRequested(
        **base,
        action_id=ActionId(_required_str(data, "action_id")),
        reason=_required_str(data, "reason"),
    )


def _construct_approval_granted(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return ApprovalGranted(**base, action_id=ActionId(_required_str(data, "action_id")))


def _construct_approval_rejected(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return ApprovalRejected(
        **base,
        action_id=ActionId(_required_str(data, "action_id")),
        reason=_required_str(data, "reason"),
    )


def _construct_operator_instruction(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return OperatorInstruction(
        **base,
        instruction=_required_str(data, "instruction"),
        amends_objective=_optional_bool(data, "amends_objective", default=False),
    )


def _optional_bool(data: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in data:
        return default
    return _required_bool(data, key)


def _construct_run_stopped(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return RunStopped(
        **base,
        reason=StopReason(_required_str(data, "reason")),
        summary=_required_str(data, "summary"),
    )


def _construct_worker_spawned(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return WorkerSpawned(
        **base,
        worker_id=WorkerId(_required_str(data, "worker_id")),
        worker_run_id=RunId(_required_str(data, "worker_run_id")),
        workspace_id=WorkspaceId(_required_str(data, "workspace_id")),
        objective=_required_str(data, "objective"),
        budget_share_cost_usd=_required_number(data, "budget_share_cost_usd"),
    )


def _construct_worker_stopped(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return WorkerStopped(
        **base,
        worker_id=WorkerId(_required_str(data, "worker_id")),
        outcome=WorkerOutcome(_required_str(data, "outcome")),
        summary=_required_str(data, "summary"),
    )


def _construct_worker_merged(base: dict[str, Any], data: dict[str, Any]) -> Event:
    return WorkerMerged(
        **base,
        worker_id=WorkerId(_required_str(data, "worker_id")),
        outcome=MergeOutcome(_required_str(data, "outcome")),
        revision=_optional_str(data, "revision"),
        detail=_required_str(data, "detail"),
    )


_CONSTRUCTORS: Final[dict[str, Callable[[dict[str, Any], dict[str, Any]], Event]]] = {
    "RunStarted": _construct_run_started,
    "PlanCreated": _construct_plan_created,
    "ActionProposed": _construct_action_proposed,
    "ActionAuthorized": _construct_action_authorized,
    "ActionRejected": _construct_action_rejected,
    "ToolExecutionStarted": _construct_tool_execution_started,
    "ToolSucceeded": _construct_tool_succeeded,
    "ToolFailed": _construct_tool_failed,
    "RetryScheduled": _construct_retry_scheduled,
    "CircuitOpened": _construct_circuit_opened,
    "VerificationPassed": _construct_verification_passed,
    "VerificationFailed": _construct_verification_failed,
    "ReflectionRecorded": _construct_reflection_recorded,
    "ContextAssembled": _construct_context_assembled,
    "ArtifactRecorded": _construct_artifact_recorded,
    "BudgetDebited": _construct_budget_debited,
    "ApprovalRequested": _construct_approval_requested,
    "ApprovalGranted": _construct_approval_granted,
    "ApprovalRejected": _construct_approval_rejected,
    "OperatorInstruction": _construct_operator_instruction,
    "RunStopped": _construct_run_stopped,
    "WorkerSpawned": _construct_worker_spawned,
    "WorkerStopped": _construct_worker_stopped,
    "WorkerMerged": _construct_worker_merged,
}


def _construct_event(event_type: str, data: dict[str, Any]) -> Event:
    base = _base(data)
    try:
        constructor = _CONSTRUCTORS[event_type]
    except KeyError as exc:
        raise UnknownEventTypeError(event_type) from exc
    return constructor(base, data)


def _proposal(data: dict[str, Any]) -> ActionProposal:
    arguments_raw: Any = data.get("arguments")
    if not isinstance(arguments_raw, dict):
        msg = "proposal arguments must be an object of string keys and values"
        raise TypeError(msg)
    arguments = cast(dict[Any, Any], arguments_raw)
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in arguments.items()):
        msg = "proposal arguments must be an object of string keys and values"
        raise TypeError(msg)
    expected_observation = data.get("expected_observation")
    if expected_observation is not None and not isinstance(expected_observation, str):
        msg = "expected_observation must be a string or null"
        raise TypeError(msg)
    validated_arguments = cast(dict[str, str], dict(arguments))
    return ActionProposal(
        action_id=ActionId(_required_str(data, "action_id")),
        tool_name=_required_str(data, "tool_name"),
        arguments=validated_arguments,
        expected_observation=expected_observation,
    )


def _tool_metadata(data: dict[str, Any]) -> ToolMetadata:
    return ToolMetadata(
        name=_required_str(data, "name"),
        risk=RiskLevel(_required_str(data, "risk")),
        required_permission=Permission(_required_str(data, "required_permission")),
        side_effect=SideEffectClass(_required_str(data, "side_effect")),
        retry=RetryClass(_required_str(data, "retry")),
        idempotency=IdempotencyClass(_required_str(data, "idempotency")),
        approval=ApprovalClass(_required_str(data, "approval")),
        timeout_seconds=_required_number(data, "timeout_seconds"),
        sensitivity=DataSensitivity(_required_str(data, "sensitivity")),
    )


def _usage(data: dict[str, Any]) -> UsageDelta:
    return UsageDelta(
        cost_usd=_required_number(data, "cost_usd"),
        input_tokens=_required_int(data, "input_tokens"),
        output_tokens=_required_int(data, "output_tokens"),
        cached_input_tokens=_required_int(data, "cached_input_tokens"),
    )


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{key} must be a string or null"
        raise TypeError(msg)
    return value


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        msg = f"{key} must be a string"
        raise TypeError(msg)
    return value


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{key} must be an integer"
        raise TypeError(msg)
    return value


def _required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        msg = f"{key} must be a boolean"
        raise TypeError(msg)
    return value


def _required_number(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        msg = f"{key} must be numeric"
        raise TypeError(msg)
    result = float(value)
    if not math.isfinite(result):
        msg = f"{key} must be finite"
        raise ValueError(msg)
    return result


def _optional_int(data: dict[str, Any], key: str, *, default: int) -> int:
    if key not in data:
        return default
    return _required_int(data, key)
