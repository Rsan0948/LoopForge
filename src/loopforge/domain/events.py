from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import (
    ArtifactKind,
    validate_artifact_content,
    validate_artifact_label,
)
from loopforge.domain.context import ContextItemSnapshot
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import ToolMetadata
from loopforge.domain.types import ActionId, EventId, RunId, StopReason, UsageDelta


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    event_id: EventId
    run_id: RunId
    occurred_at: datetime
    sequence: int
    caused_by: EventId | None = None

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            msg = "event sequence must be positive"
            raise ValueError(msg)
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            msg = "occurred_at must be timezone-aware"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStarted(DomainEvent):
    objective: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanCreated(DomainEvent):
    plan: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionProposed(DomainEvent):
    proposal: ActionProposal


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionAuthorized(DomainEvent):
    proposal: ActionProposal
    tool_metadata: ToolMetadata


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionRejected(DomainEvent):
    proposal: ActionProposal
    reason_code: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionStarted(DomainEvent):
    action_id: ActionId
    attempt: int
    idempotency_key: str | None

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        if self.attempt <= 0:
            msg_2 = "tool attempt must be positive"
            raise ValueError(msg_2)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSucceeded(DomainEvent):
    action_id: ActionId
    observation: str
    attempt: int = 1

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        if self.attempt <= 0:
            msg_3 = "tool attempt must be positive"
            raise ValueError(msg_3)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFailed(DomainEvent):
    action_id: ActionId
    error_code: str
    error_message: str
    failure_class: ToolFailureClass
    attempt: int = 1

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        if self.attempt <= 0:
            msg_4 = "tool attempt must be positive"
            raise ValueError(msg_4)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryScheduled(DomainEvent):
    action_id: ActionId
    next_attempt: int
    delay_seconds: float
    reason_code: str

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        if self.next_attempt <= 1:
            msg_5 = "retry next_attempt must be greater than one"
            raise ValueError(msg_5)
        if not math.isfinite(self.delay_seconds):
            msg_7 = "retry delay must be finite"
            raise ValueError(msg_7)
        if self.delay_seconds < 0:
            msg_6 = "retry delay cannot be negative"
            raise ValueError(msg_6)


@dataclass(frozen=True, slots=True, kw_only=True)
class CircuitOpened(DomainEvent):
    tool_name: str
    reason_code: str


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationPassed(DomainEvent):
    summary: str


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationFailed(DomainEvent):
    summary: str
    score: float | None = None

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        if self.score is not None and (
            not math.isfinite(self.score) or not 0.0 <= self.score <= 1.0
        ):
            msg_9 = "verification score must be a finite fraction in [0, 1]"
            raise ValueError(msg_9)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReflectionRecorded(DomainEvent):
    reflection: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextAssembled(DomainEvent):
    """Durable record of the exact context artifact sent to the model.

    Secret-sensitivity items are rejected at construction (see
    ContextItemSnapshot); redaction before telemetry export is a later cycle.
    The prompt template id/version, when the builder used a versioned
    template, are recorded here as execution metadata.
    """

    context_items: tuple[ContextItemSnapshot, ...]
    prompt_template_id: str | None = None
    prompt_template_version: str | None = None

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        if (self.prompt_template_id is None) != (self.prompt_template_version is None):
            msg = "prompt template id and version must be recorded together"
            raise ValueError(msg)
        if self.prompt_template_id is not None and not self.prompt_template_id.strip():
            msg_2 = "prompt template id cannot be empty"
            raise ValueError(msg_2)
        if self.prompt_template_version is not None and not self.prompt_template_version.strip():
            msg_3 = "prompt template version cannot be empty"
            raise ValueError(msg_3)


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactRecorded(DomainEvent):
    """Durable workload-evidence artifact recorded alongside verification.

    Artifacts (for example, the exact workspace patch and changed-file
    inventory) make a run's evidence replayable from the authoritative event
    stream. They are evidence-only: the reducer projects them without any
    control-state effect, and their kind vocabulary is code-owned and closed.
    """

    kind: ArtifactKind
    label: str
    content: str

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        if not isinstance(self.kind, ArtifactKind):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_10 = "artifact kind must be an ArtifactKind"
            raise TypeError(msg_10)
        validate_artifact_label(self.label)
        validate_artifact_content(self.content)


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetDebited(DomainEvent):
    usage: UsageDelta


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequested(DomainEvent):
    action_id: ActionId
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalGranted(DomainEvent):
    action_id: ActionId


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStopped(DomainEvent):
    reason: StopReason
    summary: str


Event = (
    RunStarted
    | PlanCreated
    | ActionProposed
    | ActionAuthorized
    | ActionRejected
    | ToolExecutionStarted
    | ToolSucceeded
    | ToolFailed
    | RetryScheduled
    | CircuitOpened
    | VerificationPassed
    | VerificationFailed
    | ReflectionRecorded
    | ContextAssembled
    | ArtifactRecorded
    | BudgetDebited
    | ApprovalRequested
    | ApprovalGranted
    | RunStopped
)
