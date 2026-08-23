from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Final

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
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    ActionRejected,
    ApprovalGranted,
    ApprovalRequested,
    BudgetDebited,
    CircuitOpened,
    DomainEvent,
    Event,
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
)
from loopforge.domain.types import (
    ActionId,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    StopReason,
    UsageDelta,
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
        BudgetDebited,
        ApprovalRequested,
        ApprovalGranted,
        RunStopped,
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
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            msg = "event envelope must be a JSON object"
            raise ValueError(msg)

        version = raw.get("schema_version")
        if version != SCHEMA_VERSION:
            msg = f"unsupported event schema version: {version!r}"
            raise UnsupportedEventSchemaError(msg)

        event_type = raw.get("event_type")
        if not isinstance(event_type, str) or event_type not in _EVENT_TYPES:
            msg = f"unknown event type: {event_type!r}"
            raise UnknownEventTypeError(msg)

        event_data = raw.get("event")
        if not isinstance(event_data, dict):
            msg = "event body must be a JSON object"
            raise ValueError(msg)

        return _construct_event(event_type, event_data)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    # StrEnum and NewType-backed strings serialize naturally. Dataclasses have
    # already been lowered by asdict().
    return value


def _base(data: dict[str, Any]) -> dict[str, Any]:
    caused_by_raw = data.get("caused_by")
    return {
        "event_id": EventId(_required_str(data, "event_id")),
        "run_id": RunId(_required_str(data, "run_id")),
        "occurred_at": datetime.fromisoformat(_required_str(data, "occurred_at")),
        "sequence": _required_int(data, "sequence"),
        "caused_by": EventId(caused_by_raw) if isinstance(caused_by_raw, str) else None,
    }


def _construct_event(event_type: str, data: dict[str, Any]) -> Event:
    base = _base(data)
    if event_type == "RunStarted":
        return RunStarted(**base, objective=_required_str(data, "objective"))
    if event_type == "PlanCreated":
        return PlanCreated(**base, plan=_required_str(data, "plan"))
    if event_type in {"ActionProposed", "ActionAuthorized", "ActionRejected"}:
        proposal_raw = data.get("proposal")
        if not isinstance(proposal_raw, dict):
            msg = "proposal must be a JSON object"
            raise ValueError(msg)
        proposal = _proposal(proposal_raw)
        if event_type == "ActionProposed":
            return ActionProposed(**base, proposal=proposal)
        if event_type == "ActionAuthorized":
            metadata_raw = data.get("tool_metadata")
            if not isinstance(metadata_raw, dict):
                msg = "tool_metadata must be a JSON object"
                raise ValueError(msg)
            return ActionAuthorized(
                **base,
                proposal=proposal,
                tool_metadata=_tool_metadata(metadata_raw),
            )
        return ActionRejected(
            **base,
            proposal=proposal,
            reason_code=_required_str(data, "reason_code"),
        )
    if event_type == "ToolExecutionStarted":
        key = data.get("idempotency_key")
        if key is not None and not isinstance(key, str):
            raise ValueError("idempotency_key must be a string or null")
        return ToolExecutionStarted(
            **base,
            action_id=ActionId(_required_str(data, "action_id")),
            attempt=_required_int(data, "attempt"),
            idempotency_key=key,
        )
    if event_type == "ToolSucceeded":
        return ToolSucceeded(
            **base,
            action_id=ActionId(_required_str(data, "action_id")),
            observation=_required_str(data, "observation"),
            attempt=_optional_int(data, "attempt", default=1),
        )
    if event_type == "ToolFailed":
        failure_raw = data.get("failure_class")
        if isinstance(failure_raw, str):
            failure_class = ToolFailureClass(failure_raw)
        else:
            # Backward-compatible decode for PACS-001/002 schema-v1 payloads.
            legacy_retryable = _required_bool(data, "retryable")
            failure_class = (
                ToolFailureClass.TRANSIENT
                if legacy_retryable
                else ToolFailureClass.PERMANENT
            )
        return ToolFailed(
            **base,
            action_id=ActionId(_required_str(data, "action_id")),
            error_code=_required_str(data, "error_code"),
            error_message=_required_str(data, "error_message"),
            failure_class=failure_class,
            attempt=_optional_int(data, "attempt", default=1),
        )
    if event_type == "RetryScheduled":
        return RetryScheduled(
            **base,
            action_id=ActionId(_required_str(data, "action_id")),
            next_attempt=_required_int(data, "next_attempt"),
            delay_seconds=_required_number(data, "delay_seconds"),
            reason_code=_required_str(data, "reason_code"),
        )
    if event_type == "CircuitOpened":
        return CircuitOpened(
            **base,
            tool_name=_required_str(data, "tool_name"),
            reason_code=_required_str(data, "reason_code"),
        )
    if event_type == "VerificationPassed":
        return VerificationPassed(**base, summary=_required_str(data, "summary"))
    if event_type == "VerificationFailed":
        score = data.get("score")
        if score is not None and not isinstance(score, (int, float)):
            msg = "score must be numeric or null"
            raise ValueError(msg)
        return VerificationFailed(
            **base,
            summary=_required_str(data, "summary"),
            score=float(score) if score is not None else None,
        )
    if event_type == "ReflectionRecorded":
        return ReflectionRecorded(**base, reflection=_required_str(data, "reflection"))
    if event_type == "BudgetDebited":
        usage_raw = data.get("usage")
        if not isinstance(usage_raw, dict):
            msg = "usage must be a JSON object"
            raise ValueError(msg)
        return BudgetDebited(**base, usage=_usage(usage_raw))
    if event_type == "ApprovalRequested":
        return ApprovalRequested(
            **base,
            action_id=ActionId(_required_str(data, "action_id")),
            reason=_required_str(data, "reason"),
        )
    if event_type == "ApprovalGranted":
        return ApprovalGranted(**base, action_id=ActionId(_required_str(data, "action_id")))
    if event_type == "RunStopped":
        return RunStopped(
            **base,
            reason=StopReason(_required_str(data, "reason")),
            summary=_required_str(data, "summary"),
        )
    raise UnknownEventTypeError(event_type)


def _proposal(data: dict[str, Any]) -> ActionProposal:
    arguments = data.get("arguments")
    if not isinstance(arguments, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in arguments.items()
    ):
        msg = "proposal arguments must be an object of string keys and values"
        raise ValueError(msg)
    expected_observation = data.get("expected_observation")
    if expected_observation is not None and not isinstance(expected_observation, str):
        msg = "expected_observation must be a string or null"
        raise ValueError(msg)
    return ActionProposal(
        action_id=ActionId(_required_str(data, "action_id")),
        tool_name=_required_str(data, "tool_name"),
        arguments=arguments,
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


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        msg = f"{key} must be a string"
        raise ValueError(msg)
    return value


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{key} must be an integer"
        raise ValueError(msg)
    return value


def _required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        msg = f"{key} must be a boolean"
        raise ValueError(msg)
    return value


def _required_number(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        msg = f"{key} must be numeric"
        raise ValueError(msg)
    return float(value)


def _optional_int(data: dict[str, Any], key: str, *, default: int) -> int:
    if key not in data:
        return default
    return _required_int(data, key)
