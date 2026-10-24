"""Durable prepared-goal control plane. All authority is supplied by trusted intake."""

from dataclasses import dataclass, replace
from enum import StrEnum

from loopforge.domain.goal_review import (
    ClarificationPolicy,
    ClarificationRequest,
    ControlAcknowledged,
    ControlRequested,
    ExperimentRecorded,
    ExperimentState,
    QuestionAnswered,
    QuestionOpened,
    QuestionState,
    ReviewRecorded,
)
from loopforge.domain.goal_review_reducer import reduce_review
from loopforge.domain.investigation import (
    FailureKind,
    InspectionRequest,
    RecoveryAttempt,
    RecoveryIntent,
    ResearchRecord,
    TaskAssignment,
)


class GoalAction(StrEnum):
    DELEGATE = "delegate"
    VERIFY = "verify"
    RETRY = "retry"
    DELIVER = "deliver"
    BLOCK = "block"
    INSPECT = "inspect"
    PREPARE = "prepare"
    REPRODUCE = "reproduce"
    RETRIEVE = "retrieve"
    ASK = "ask"


@dataclass(frozen=True)
class GoalActionPolicy:
    risk: str
    permission: str
    side_effect: bool
    retry: str
    idempotency: str
    approval: str
    timeout_seconds: int
    sensitivity: str


# Proposals contain none of these fields. No arbitrary commands, paths or scope.
GOAL_ACTIONS = {
    action: GoalActionPolicy(
        risk="bounded-local"
        if action
        in {
            GoalAction.DELEGATE,
            GoalAction.VERIFY,
            GoalAction.RETRY,
            GoalAction.PREPARE,
            GoalAction.REPRODUCE,
        }
        else "read-only",
        permission="prepared-task",
        side_effect=action
        in {
            GoalAction.DELEGATE,
            GoalAction.VERIFY,
            GoalAction.RETRY,
            GoalAction.PREPARE,
            GoalAction.REPRODUCE,
        },
        retry="reconcile-before-retry",
        idempotency="goal-decision-key",
        approval="operator-frozen-contract",
        timeout_seconds=43_200,
        sensitivity="repository-confidential",
    )
    for action in GoalAction
}


def count(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        msg = f"{name} must be an integer >= {minimum}"
        raise ValueError(msg)


@dataclass(frozen=True)
class GoalPolicy:
    objective: str
    contract_sha256: str
    max_cost_microusd: int
    deadline_epoch: int
    max_decisions: int = 12
    max_failures: int = 3
    consecutive_failures: bool = False
    max_action_rejections: int = 3
    task_intent: str = "bugfix"
    require_recovery_evidence: bool = False
    max_assignment_versions: int = 8
    remote_data: str = "none"
    worker_binding_sha256: str = ""
    investigation_enabled: bool = False
    investigation_contract: str = ""
    max_research_failures: int = 3
    max_stagnant_decisions: int = 4
    approved_environment_ids: tuple[str, ...] = ()
    checkpoint_revision: str = ""
    clarifications: tuple[ClarificationPolicy, ...] = ()
    dynamic_clarifications: tuple[ClarificationPolicy, ...] = ()

    def __post_init__(self) -> None:
        if not self.objective.strip() or len(self.objective) > 8000:
            msg = "Goal requires a bounded objective"
            raise ValueError(msg)
        if len(self.contract_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.contract_sha256
        ):
            msg = "Goal requires a frozen contract hash"
            raise ValueError(msg)
        if self.worker_binding_sha256 and (
            len(self.worker_binding_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.worker_binding_sha256)
        ):
            msg = "Invalid prepared worker binding"
            raise ValueError(msg)
        if len({p.topic for p in self.clarifications}) != len(self.clarifications):
            msg = "Clarification topics must be unique"
            raise ValueError(msg)
        if type(self.consecutive_failures) is not bool:
            msg = "Failure counting mode requires a boolean"
            raise ValueError(msg)
        if type(self.investigation_enabled) is not bool:
            msg = "Investigation authority requires a boolean"
            raise ValueError(msg)
        for name in (
            "max_assignment_versions",
            "max_action_rejections",
            "max_cost_microusd",
            "deadline_epoch",
            "max_decisions",
            "max_failures",
            "max_research_failures",
            "max_stagnant_decisions",
        ):
            count(getattr(self, name), name, minimum=1)
        if self.task_intent not in {"bugfix", "feature", "launch"}:
            msg_0 = "Unknown task intent"
            raise ValueError(msg_0)
        if self.max_cost_microusd > 5_000_000 or self.remote_data not in {
            "none",
            "synthetic-fixture",
            "prepared-repository",
        }:
            msg = "Goal exceeds supported authority"
            raise ValueError(msg)


@dataclass(frozen=True)
class SupervisorDecision:
    action: GoalAction
    rationale: str
    plan: str
    inspection: InspectionRequest | None = None
    environment: str | None = None
    assignment: TaskAssignment | None = None
    evidence_id: str | None = None
    question: ClarificationRequest | None = None
    recovery: RecoveryIntent | None = None

    def __post_init__(self) -> None:
        if type(self.action) is not GoalAction or any(
            not text.strip() or len(text) > 4000 for text in (self.rationale, self.plan)
        ):
            msg = "Invalid supervisor decision"
            raise ValueError(msg)
        supplied = {
            name
            for name in (
                "inspection",
                "environment",
                "assignment",
                "evidence_id",
                "question",
                "recovery",
            )
            if getattr(self, name) is not None
        }
        expected = {
            GoalAction.ASK: {"question"},
            GoalAction.INSPECT: {"inspection"},
            GoalAction.PREPARE: {"environment"},
            GoalAction.RETRIEVE: {"evidence_id"},
        }.get(self.action, set())
        if self.action == GoalAction.RETRY and self.recovery is not None:
            expected = {"recovery"}
        if self.action == GoalAction.DELEGATE and self.assignment is not None:
            expected = {"assignment"}
        if self.action == GoalAction.RETRIEVE and self.inspection is not None:
            expected = {"evidence_id", "inspection"}
        if (
            supplied != expected
            or (self.environment is not None and not self.environment.strip())
            or (self.evidence_id is not None and not self.evidence_id.strip())
        ):
            accepted = ", ".join(sorted(expected)) or "no arguments"
            msg = (
                "Supervisor action arguments do not match its capability: "
                f"{self.action.value} accepts {accepted}"
            )
            raise ValueError(msg)


@dataclass(frozen=True)
class WorkerObservation:
    status: str = "absent"
    evidence: str = ""
    candidate_sha256: str = ""
    verified: bool = False
    failure: FailureKind = FailureKind.NONE
    artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.status
            not in {
                "absent",
                "queued",
                "coding",
                "review_pending",
                "reviewing",
                "patch_ready",
                "attention",
                "accepted",
                "rejected",
            }
            or type(self.verified) is not bool
        ):
            msg = "Invalid worker observation"
            raise ValueError(msg)
        if self.verified and (
            self.status != "patch_ready"
            or len(self.candidate_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.candidate_sha256)
        ):
            msg = "Verified delivery requires a candidate identity"
            raise ValueError(msg)


@dataclass(frozen=True)
class GoalCreated:
    policy: GoalPolicy


@dataclass(frozen=True)
class CallReserved:
    key: str
    maximum_microusd: int
    trace: str = ""


@dataclass(frozen=True)
class DecisionRecorded:
    decision: SupervisorDecision
    charged_microusd: int
    trace: str


@dataclass(frozen=True)
class CallFailed:
    charged_microusd: int
    reason: str
    retry_at_epoch: int


@dataclass(frozen=True)
class ActionObserved:
    observation: WorkerObservation


@dataclass(frozen=True)
class ResearchRecorded:
    record: ResearchRecord


@dataclass(frozen=True)
class ActionRejected:
    category: FailureKind
    summary: str


@dataclass(frozen=True)
class GoalStopped:
    status: str
    summary: str


GoalEvent = (
    GoalCreated
    | CallReserved
    | DecisionRecorded
    | CallFailed
    | ActionObserved
    | ResearchRecorded
    | GoalStopped
    | ActionRejected
    | QuestionOpened
    | QuestionAnswered
    | ExperimentRecorded
    | ControlRequested
    | ControlAcknowledged
    | ReviewRecorded
)


@dataclass(frozen=True)
class AssignmentVersion:
    assignment: TaskAssignment
    environment_id: str
    checkpoint: str = ""
    candidate_sha256: str = ""
    verified: bool = False
    branch: str = ""
    seed_manifest_sha256: str = ""


@dataclass(frozen=True)
class GoalState:
    policy: GoalPolicy | None = None
    phase: str = "new"
    sequence: int = 0
    spent_microusd: int = 0
    reserved_microusd: int = 0
    call_key: str = ""
    call_keys: tuple[str, ...] = ()
    decisions: int = 0
    failures: int = 0
    retry_at_epoch: int = 0
    decision: SupervisorDecision | None = None
    observation: WorkerObservation = WorkerObservation()
    summary: str = ""
    plan: str = ""
    research: tuple[ResearchRecord, ...] = ()
    environment_id: str = ""
    environment_ready: bool = False
    baseline_reproduced: bool = False
    assignment: TaskAssignment | None = None
    assignment_versions: tuple[AssignmentVersion, ...] = ()
    action_rejections: int = 0
    research_failures: int = 0
    stagnant_decisions: int = 0
    questions: tuple[QuestionState, ...] = ()
    dynamic_questions: tuple[ClarificationPolicy, ...] = ()
    last_verified_candidate: str = ""
    recoveries: tuple[RecoveryAttempt, ...] = ()
    experiments: tuple[ExperimentState, ...] = ()
    control_requested: str = ""
    paused: bool = False
    late_answer_conflict: bool = False
    disposition: str = "pending"
    accepted_commit: str = ""

    @property
    def terminal(self) -> bool:
        return self.phase in {"delivered", "blocked", "cancelled"}


def allowed_actions(observation: WorkerObservation) -> frozenset[GoalAction]:
    actions = {GoalAction.BLOCK}
    if observation.status == "absent":
        actions.add(GoalAction.DELEGATE)
    if observation.status == "review_pending":
        actions.add(GoalAction.VERIFY)
    if observation.status == "attention":
        actions.add(GoalAction.RETRY)
    if observation.status == "patch_ready" and observation.verified:
        actions.add(GoalAction.DELIVER)
    return frozenset(actions)


def current_recovery(state: GoalState) -> RecoveryIntent | None:
    """The validated retry hypothesis that applies to the current assignment, if any."""
    if state.decision and state.decision.action == GoalAction.RETRY and state.decision.recovery:
        return state.decision.recovery
    if state.recoveries and state.recoveries[-1].assignment_version == len(
        state.assignment_versions
    ):
        return state.recoveries[-1].intent
    return None


def goal_allowed_actions(state: GoalState) -> frozenset[GoalAction]:
    actions = set(allowed_actions(state.observation))
    if state.policy and state.policy.dynamic_clarifications and len(state.dynamic_questions) < 8:
        actions.add(GoalAction.ASK)
    if state.policy and state.policy.investigation_enabled:
        actions.update({GoalAction.INSPECT, GoalAction.RETRIEVE})
        if state.observation.status in {"attention", "patch_ready", "review_pending"}:
            actions.add(GoalAction.PREPARE)
            if state.environment_ready:
                actions.add(GoalAction.REPRODUCE)
            if state.environment_ready and state.baseline_reproduced:
                actions.add(GoalAction.DELEGATE)
        if state.observation.status == "absent":
            actions.discard(GoalAction.DELEGATE)
            if state.assignment is None:
                actions.add(GoalAction.PREPARE)
                if state.environment_ready:
                    actions.add(GoalAction.REPRODUCE)
            if state.environment_ready and state.baseline_reproduced:
                actions.add(GoalAction.DELEGATE)
    if state.policy and (
        any(p.topic not in {q.topic for q in state.questions} for p in state.policy.clarifications)
        or any(
            not q.answer and not any(e.topic == q.topic for e in state.experiments)
            for q in state.questions
        )
    ):
        actions.difference_update(
            {GoalAction.DELEGATE, GoalAction.VERIFY, GoalAction.RETRY, GoalAction.DELIVER}
        )
    if (
        state.assignment_versions
        and state.experiments
        and state.assignment_versions[-1].branch != state.experiments[-1].branch
    ):
        actions.difference_update({GoalAction.RETRY, GoalAction.VERIFY, GoalAction.DELIVER})
    return frozenset(actions)


def reduce_goal(state: GoalState, event: GoalEvent) -> GoalState:  # noqa: PLR0912, PLR0915 - explicit boundary dispatch/normalization
    """Reject illegal history, changed authority, overspending and false completion."""
    reviewed = reduce_review(state, event)
    if reviewed is not None:
        return replace(reviewed, sequence=state.sequence + 1)
    if state.terminal:
        msg = "Terminal goals cannot transition"
        raise ValueError(msg)
    result: GoalState | None = None
    if isinstance(event, GoalCreated) and state.phase == "new":
        result = replace(state, policy=event.policy, phase="ready")
    if isinstance(event, CallReserved) and state.phase == "ready" and state.policy:
        if state.paused or state.control_requested or state.late_answer_conflict:
            msg = "Goal control must be reconciled before inference"
            raise ValueError(msg)
        count(event.maximum_microusd, "reservation", minimum=1)
        if (
            not event.key
            or event.key in state.call_keys
            or state.decisions >= state.policy.max_decisions
            or state.failures >= state.policy.max_failures
            or state.action_rejections >= state.policy.max_action_rejections
            or state.spent_microusd + event.maximum_microusd > state.policy.max_cost_microusd
        ):
            msg = "Goal inference allowance exhausted"
            raise ValueError(msg)
        result = replace(
            state,
            phase="inference",
            reserved_microusd=event.maximum_microusd,
            call_key=event.key,
            call_keys=(*state.call_keys, event.key),
            decisions=state.decisions + 1,
        )
    if isinstance(event, (DecisionRecorded, CallFailed)) and state.phase == "inference":
        count(event.charged_microusd, "charge")
        # Unknown outcomes charge the full reservation. Never reduce a known charge.
        if event.charged_microusd > state.reserved_microusd:
            msg = "Provider charge exceeds reserved maximum"
            raise ValueError(msg)
        result = replace(
            state,
            spent_microusd=state.spent_microusd + event.charged_microusd,
            reserved_microusd=0,
        )
        if isinstance(event, DecisionRecorded):
            result = replace(
                result,
                failures=0
                if state.policy and state.policy.consecutive_failures
                else state.failures,
                phase="ready"
                if state.control_requested or state.late_answer_conflict
                else "action",
                decision=None
                if state.control_requested or state.late_answer_conflict
                else event.decision,
                plan=event.decision.plan,
            )
        else:
            count(event.retry_at_epoch, "retry deadline")
            result = replace(
                result,
                phase="ready",
                failures=state.failures + 1,
                retry_at_epoch=event.retry_at_epoch,
                summary=event.reason,
            )
    if isinstance(event, ActionRejected) and state.phase == "action" and state.policy:
        if not event.summary.strip() or len(event.summary) > 1000:
            msg_0 = "Rejected action needs bounded actionable feedback"
            raise ValueError(msg_0)
        result = replace(
            state,
            phase="ready",
            decision=None,
            action_rejections=state.action_rejections + 1,
            summary=event.summary,
        )
    if isinstance(event, ActionObserved) and state.phase in {"ready", "action"}:
        result = replace(
            state,
            phase="ready",
            observation=event.observation,
            decision=None,
            recoveries=(
                *state.recoveries,
                RecoveryAttempt(
                    state.observation.candidate_sha256,
                    state.decision.recovery,
                    len(state.assignment_versions),
                ),
            )
            if state.decision
            and state.decision.action == GoalAction.RETRY
            and state.decision.recovery
            else state.recoveries,
            stagnant_decisions=0
            if event.observation.candidate_sha256
            and event.observation.candidate_sha256 != state.observation.candidate_sha256
            else state.stagnant_decisions,
            last_verified_candidate=event.observation.candidate_sha256
            if event.observation.verified
            else state.last_verified_candidate,
        )
    if (
        isinstance(event, ResearchRecorded)
        and state.phase == "action"
        and state.policy
        and state.policy.investigation_enabled
    ):
        result = _reduce_research(state, event.record)
    if isinstance(event, GoalStopped) and state.policy:
        if event.status not in {"delivered", "blocked", "cancelled"} or not event.summary.strip():
            msg = "Invalid goal disposition"
            raise ValueError(msg)
        if event.status == "delivered" and not (
            state.phase == "action"
            and state.decision
            and state.decision.action == GoalAction.DELIVER
            and state.observation.verified
            and GoalAction.DELIVER in goal_allowed_actions(state)
            and not state.control_requested
            and not state.paused
            and not state.late_answer_conflict
        ):
            msg = "Delivery requires independent verified candidate evidence"
            raise ValueError(msg)
        if state.reserved_microusd:
            msg = "Reconcile outstanding inference before stopping"
            raise ValueError(msg)
        result = replace(
            state, phase=event.status, summary=event.summary, control_requested="", paused=False
        )
    if result is None:
        msg = f"Illegal goal transition from {state.phase}: {type(event).__name__}"
        raise ValueError(msg)
    return replace(result, sequence=state.sequence + 1)


def _reduce_research(state: GoalState, record: ResearchRecord) -> GoalState:
    if record.operation_key != state.call_key or any(
        r.operation_key == record.operation_key for r in state.research
    ):
        msg = "Investigation result must match one unacknowledged decision"
        raise ValueError(msg)
    decision = state.decision
    assert decision is not None
    policy = state.policy
    assert policy is not None
    if record.candidate_sha256 and record.candidate_sha256 != state.observation.candidate_sha256:
        msg_0 = "Candidate evidence is stale"
        raise ValueError(msg_0)
    if record.revision != policy.checkpoint_revision:
        msg = "Investigation evidence must match the authorized checkpoint"
        raise ValueError(msg)
    if record.kind == "environment" and (
        state.observation.status in {"coding", "reviewing", "queued"}
        or record.environment_id != decision.environment
        or record.environment_id not in policy.approved_environment_ids
    ):
        msg = "Environment measurement does not match approved proposed authority"
        raise ValueError(msg)
    permitted_kind = {
        GoalAction.INSPECT: {"inventory", "source"},
        GoalAction.PREPARE: {"environment"},
        GoalAction.REPRODUCE: {"baseline"},
        GoalAction.RETRIEVE: {"retrieval"},
        GoalAction.DELEGATE: {"assignment"},
    }.get(decision.action, set())
    if record.kind not in permitted_kind or (
        record.assignment is not None and record.assignment != decision.assignment
    ):
        msg = "Investigation record does not match proposed action"
        raise ValueError(msg)
    if record.kind == "assignment" and (
        not state.baseline_reproduced
        or not state.environment_ready
        or state.observation.status in {"coding", "reviewing", "queued"}
        or len(state.assignment_versions) >= policy.max_assignment_versions
        or (state.assignment is not None and not record.candidate_checkpoint)
    ):
        msg = "Assignment needs a measured baseline, idle worker and preserved checkpoint"
        raise ValueError(msg)
    if (
        record.baseline_reproduced
        and record.failure == FailureKind.NONE
        and policy.task_intent == "bugfix"
    ):
        msg_1 = "Bug reproduction requires an observed behavioral failure"
        raise ValueError(msg_1)
    if record.kind == "baseline" and (
        not state.environment_ready or record.environment_id != state.environment_id
    ):
        msg = "Baseline evidence requires the measured approved environment"
        raise ValueError(msg)
    repeated = any(
        r.kind == record.kind
        and r.content_sha256 == record.content_sha256
        and (
            record.end_offset_bytes == 0
            or (
                r.start_offset_bytes <= record.start_offset_bytes
                and r.end_offset_bytes >= record.end_offset_bytes
            )
        )
        for r in state.research
    )
    result = replace(
        state,
        research=(*state.research, record),
        stagnant_decisions=state.stagnant_decisions + 1 if repeated else 0,
        research_failures=state.research_failures
        + int(record.failure not in {FailureKind.NONE, FailureKind.VERIFICATION}),
        phase="action" if record.kind == "assignment" else "ready",
        decision=decision if record.kind == "assignment" else None,
    )
    if record.kind == "environment":
        result = replace(
            result,
            environment_id=record.environment_id,
            environment_ready=record.environment_ready,
            baseline_reproduced=False,
        )
    if record.kind == "baseline":
        result = replace(result, baseline_reproduced=record.baseline_reproduced)
    if record.assignment is not None:
        result = replace(
            result,
            assignment=record.assignment,
            assignment_versions=(
                *state.assignment_versions,
                AssignmentVersion(
                    record.assignment,
                    state.environment_id,
                    record.candidate_checkpoint,
                    record.candidate_sha256,
                    state.observation.verified,
                    state.experiments[-1].branch if state.experiments else "",
                    record.seed_manifest_sha256,
                ),
            ),
            observation=WorkerObservation(),
        )
    return result
