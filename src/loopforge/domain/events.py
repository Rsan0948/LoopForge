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
from loopforge.domain.orchestration import (
    MergeOutcome,
    WorkerOutcome,
    validate_budget_share,
    validate_worker_id,
    validate_worker_text,
)
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import ToolMetadata
from loopforge.domain.types import (
    ActionId,
    EventId,
    RunId,
    StopReason,
    UsageDelta,
    WorkerId,
    WorkspaceId,
)


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
class ModelTurnRecorded(DomainEvent):
    """Durable per-turn model-identity record (PACS-015).

    Recorded on every successful model turn: which provider/model produced
    the turn and which action the turn yielded. Evidence-only — the reducer
    projects it without any control-state effect — so the derived provenance
    graph can attribute every proposed action to the model that produced it
    from the authoritative stream alone. Identity comes from the adapter's
    code-owned ``ModelCapabilities``, never from model output.
    """

    provider: str
    model: str
    action_id: ActionId

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        if not isinstance(self.provider, str) or not self.provider.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_11 = "model turn provider cannot be empty"
            raise ValueError(msg_11)
        if not isinstance(self.model, str) or not self.model.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_12 = "model turn model cannot be empty"
            raise ValueError(msg_12)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequested(DomainEvent):
    action_id: ActionId
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalGranted(DomainEvent):
    action_id: ActionId


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRejected(DomainEvent):
    """Durable operator denial of a pending approval-gated action.

    The reducer clears the pending proposal and returns the run to READY so
    the drive cycle can re-plan; the reason is operator-authored text bounded
    and sanitized at construction before it enters the durable stream.
    """

    action_id: ActionId
    reason: str

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        validate_operator_text(self.reason, "approval rejection reason")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorInstruction(DomainEvent):
    """Durable operator steering instruction for a non-terminal run.

    Operator authority (AGENTS.md rule 16): instructions arrive only through
    this event — the runtime never accepts silent out-of-band steering. When
    ``amends_objective`` is set the instruction replaces the run objective in
    the reducer projection; budgets, permissions, and sandbox boundaries are
    unaffected (rule 11 — instructions can never amend authority mid-run).
    """

    instruction: str
    amends_objective: bool = False

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        validate_operator_text(self.instruction, "operator instruction")


_MAX_OPERATOR_TEXT = 4000


def validate_operator_text(value: str, field: str) -> None:
    """Bound and sanitize operator-authored text for durable events."""
    if not value.strip():
        msg = f"{field} cannot be empty"
        raise ValueError(msg)
    if len(value) > _MAX_OPERATOR_TEXT:
        msg_2 = f"{field} exceeds {_MAX_OPERATOR_TEXT} characters"
        raise ValueError(msg_2)
    if any((ord(char) < 0x20 and char not in "\n\t") or ord(char) == 0x7F for char in value):
        msg_3 = f"{field} must not contain control characters"
        raise ValueError(msg_3)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStopped(DomainEvent):
    reason: StopReason
    summary: str


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerSpawned(DomainEvent):
    """Durable worker ownership record on the orchestrator's run stream.

    Recorded when the orchestrator spawns a worker: which worker owns which
    workspace, which run stream it drives, and its static cost-budget share
    of the global limit (a share, never new authority — AGENTS.md rule 12).
    """

    worker_id: WorkerId
    worker_run_id: RunId
    workspace_id: WorkspaceId
    objective: str
    budget_share_cost_usd: float

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        validate_worker_id(str(self.worker_id))
        validate_worker_id(str(self.workspace_id))
        validate_worker_text(self.objective, "worker objective")
        validate_budget_share(self.budget_share_cost_usd)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerStopped(DomainEvent):
    """Durable record of a worker run reaching a terminal state."""

    worker_id: WorkerId
    outcome: WorkerOutcome
    summary: str

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        validate_worker_id(str(self.worker_id))
        if not isinstance(self.outcome, WorkerOutcome):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_11 = "worker outcome must be a WorkerOutcome"
            raise TypeError(msg_11)
        validate_worker_text(self.summary, "worker stop summary")


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerMerged(DomainEvent):
    """Durable reconciliation record for one worker's worktree branch.

    ``MERGED`` carries the merge commit revision; ``CONFLICT`` carries none
    (a conflicted merge is aborted, never silently resolved). Conflicting
    state updates are therefore rejected explicitly and replayably.
    """

    worker_id: WorkerId
    outcome: MergeOutcome
    revision: str | None
    detail: str

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        validate_worker_id(str(self.worker_id))
        if not isinstance(self.outcome, MergeOutcome):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_12 = "merge outcome must be a MergeOutcome"
            raise TypeError(msg_12)
        if self.outcome is MergeOutcome.MERGED:
            if self.revision is None or not self.revision.strip():
                msg_13 = "a merged outcome must carry the merge revision"
                raise ValueError(msg_13)
        elif self.revision is not None:
            msg_14 = "a conflict outcome cannot carry a merge revision"
            raise ValueError(msg_14)
        if self.revision is not None and _has_unsafe_revision(self.revision):
            msg_15 = "merge revision must not contain control characters"
            raise ValueError(msg_15)
        validate_worker_text(self.detail, "worker merge detail")


def _has_unsafe_revision(revision: str) -> bool:
    return len(revision) > 128 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in revision)


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
    | ModelTurnRecorded
    | ApprovalRequested
    | ApprovalGranted
    | ApprovalRejected
    | OperatorInstruction
    | RunStopped
    | WorkerSpawned
    | WorkerStopped
    | WorkerMerged
)
