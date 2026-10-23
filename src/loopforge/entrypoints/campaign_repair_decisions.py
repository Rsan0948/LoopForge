"""Observable repair commitments, projected from events without retaining chat."""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loopforge.adapters.gemini_repair import RepairDiagnosis
from loopforge.adapters.repair_journal import RepairJournal
from loopforge.domain.context import ContextItem, ContextSource, ModelContext, ModelRole
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.events import ActionProposed, Event, ToolSucceeded
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.security import TrustClass
from loopforge.domain.state import RunState, replay
from loopforge.domain.tooling import (
    ApprovalClass,
    DataSensitivity,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ContextItemId, Permission, RiskLevel
from loopforge.ports.context import ContextBuilderPort
from loopforge.ports.model import (
    ModelFailureClass,
    ModelPort,
    ModelToolSpec,
    ModelTurn,
    ModelTurnError,
)
from loopforge.ports.state_store import StateStorePort
from loopforge.ports.tools import ToolExecutionRequest, ToolExecutorPort, ToolResult

DECISION_TOOL = "record_repair_decision"
INVESTIGATION = frozenset({"read_file", "search_files", "repository_map", "find_symbol"})
EDIT_ACTIONS = frozenset({"apply_edits", "write_file", "edit_file", "format_python_imports"})
POLICY_REFERENCE = "runtime:repair-decision-policy"


class RepairDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    target_defect: str = Field(min_length=1, max_length=500)
    path: str = Field(min_length=1, max_length=500)
    missing_evidence: str = Field(min_length=1, max_length=500)
    next_action: str = Field(min_length=1, max_length=500)
    best_guess: str = Field(default="", max_length=1000)
    status: Literal["investigate", "edit", "verify", "unable"]


class DecisionRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    cycle_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: RepairDecision


def decision_spec(*, force_attempt: bool = False) -> ModelToolSpec:
    schema = RepairDecision.model_json_schema()
    if force_attempt:
        schema["required"].append("best_guess")
        schema["properties"]["best_guess"]["minLength"] = 1
    return ModelToolSpec(
        name=DECISION_TOOL,
        description="Record a brief observable repair decision before work: one defect/path, "
        "missing evidence (or 'none'), and the action that evidence enables. "
        "State your best_guess concrete code change, even when uncertain. "
        "This is a concise work plan, not private reasoning. Status unable reports a blocker.",
        parameters=schema,
    )


@dataclass(frozen=True)
class DecisionProjection:
    phase: Literal["decision_required", "investigate", "act", "unable"]
    reads_used: int
    record: DecisionRecord | None


class RepairDecisionState:
    def __init__(
        self,
        store: StateStorePort,
        journal: RepairJournal,
        identity: Callable[[], str],
        *,
        force_attempt: bool = False,
        revisable: bool = False,
    ) -> None:
        self.store, self.journal, self.identity = store, journal, identity
        self.force_attempt = force_attempt
        self.revisable = revisable
        self.investigation_limit = 2 if force_attempt else 3

    def cycle_key(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [
                    self.identity(),
                    hashlib.sha256((self.journal.latest() or "").encode()).hexdigest(),
                ]
            ).encode()
        ).hexdigest()

    def applied_specialist_action(self) -> dict[str, str] | None:
        advice = json.loads(self.journal.latest() or "{}")
        if "diagnosis" not in advice:
            return None
        patch = RepairDiagnosis.model_validate(advice["diagnosis"]).patch
        if patch is None:
            return None
        for run in self.store.list_runs():
            events = self.store.events_for(run.run_id)
            proposals = {
                e.proposal.action_id: e.proposal for e in events if isinstance(e, ActionProposed)
            }
            for event in reversed(events):
                if not isinstance(event, ToolSucceeded):
                    continue
                proposal = proposals.get(event.action_id)
                if (
                    proposal is not None
                    and str(proposal.action_id).startswith("repair-guidance-")
                    and proposal.tool_name == "apply_edits"
                    and proposal.arguments.get("path") == patch.path
                    and proposal.arguments.get("sha256") == patch.sha256
                    and json.loads(proposal.arguments.get("edits", "[]"))
                    == [{"old": patch.old, "new": patch.new}]
                ):
                    return {
                        "run_id": str(run.run_id),
                        "action_id": str(event.action_id),
                        "path": patch.path,
                        "status": "historically_applied; check current verification",
                    }
        return None

    def project(self, events: Sequence[Event]) -> DecisionProjection:
        proposals = {
            e.proposal.action_id: e.proposal for e in events if isinstance(e, ActionProposed)
        }
        matching: DecisionRecord | None = None
        sequence = 0
        cycle = self.cycle_key()
        for event in events:
            if not isinstance(event, ToolSucceeded):
                continue
            proposal = proposals.get(event.action_id)
            if proposal is None or proposal.tool_name != DECISION_TOOL:
                continue
            record = DecisionRecord.model_validate_json(event.observation)
            if record.cycle_key == cycle:
                if matching is None:
                    sequence = event.sequence
                matching = record
        if matching is None:
            return DecisionProjection("decision_required", 0, None)
        reads = sum(
            isinstance(e, ActionProposed)
            and e.sequence > sequence
            and (
                e.proposal.tool_name in INVESTIGATION
                or (
                    self.force_attempt
                    and e.proposal.tool_name not in EDIT_ACTIONS | {DECISION_TOOL}
                )
            )
            for e in events
        )
        phase = (
            "unable"
            if matching.decision.status == "unable"
            else "act"
            if reads >= self.investigation_limit
            else "investigate"
        )
        return DecisionProjection(phase, reads, matching)


class RepairDecisionContext:
    def __init__(self, delegate: ContextBuilderPort, state: RepairDecisionState) -> None:
        self.delegate, self.state = delegate, state

    def build_context(
        self,
        state: RunState,
        *,
        role: ModelRole = ModelRole.CONTROLLER,
        token_budget: ContextTokenBudget | None = None,
    ) -> ModelContext:
        context = self.delegate.build_context(state, role=role, token_budget=token_budget)
        projection = self.state.project(self.state.store.events_for(state.run_id))
        if projection.phase == "decision_required":
            instruction = (
                "Call record_repair_decision once for ONE remaining defect/path. "
                "Include the concrete best_guess source change and missing evidence. "
                "Give a short work plan, not hidden reasoning."
            )
        else:
            instruction = (
                "The repair decision is already recorded below. Do not repeat startup. "
                "Use current evidence to perform the next repair action. "
            )
            if self.state.revisable:
                instruction += (
                    "If evidence disproves the plan, you may revise the decision; "
                    "revision does not refresh the investigation allowance. "
                )
            else:
                instruction += "Only status unable may be recorded again in this cycle. "
            if projection.phase == "act":
                instruction += (
                    "Investigation allowance exhausted. Attempt a scoped source edit "
                    "with current exact source, or record inability if evidence is insufficient. "
                    if self.state.force_attempt
                    else "Investigation allowance exhausted. Edit, verify, or record inability. "
                )
            else:
                remaining = self.state.investigation_limit - projection.reads_used
                instruction += f"Investigation actions remaining: {remaining}. "
        instruction += (
            "Successful earlier edits are historical facts, not pending assignments. "
            "A successful edit alone does not prove a defect fixed."
        )
        policy = ContextItem(
            item_id=ContextItemId(f"{state.run_id}:repair-decision-policy"),
            content=json.dumps(
                {
                    "phase": projection.phase,
                    "investigation_actions_used": projection.reads_used,
                    "investigation_limit": self.state.investigation_limit,
                    "force_attempt": self.state.force_attempt,
                    "revisable": self.state.revisable,
                    "instruction": instruction,
                    "continuity": "The decision persists across fresh contexts. Rewording it "
                    "or running unchanged checks cannot refresh investigation allowance. "
                    "Specialist advice remains an unverified hypothesis.",
                }
            ),
            trust=TrustClass.RUNTIME_POLICY,
            source=ContextSource(origin=TrustClass.RUNTIME_POLICY, reference=POLICY_REFERENCE),
            created_at=context.assembled_at,
        )
        items = [*context.items, policy]
        receipt = self.state.applied_specialist_action()
        if receipt is not None:
            items.append(
                ContextItem(
                    item_id=ContextItemId(f"{state.run_id}:specialist-patch-receipt"),
                    content=json.dumps(receipt),
                    trust=TrustClass.DETERMINISTIC_OBSERVATION,
                    source=ContextSource(
                        origin=TrustClass.DETERMINISTIC_OBSERVATION,
                        reference="events:matched-specialist-patch-outcome",
                        detail="Exact edit matched ToolSucceeded; does not prove the bug is fixed",
                    ),
                    created_at=context.assembled_at,
                )
            )
        if projection.record is not None:
            items.append(
                ContextItem(
                    item_id=ContextItemId(f"{state.run_id}:repair-decision"),
                    content=projection.record.model_dump_json(),
                    trust=TrustClass.MODEL_INFERENCE,
                    source=ContextSource(
                        origin=TrustClass.MODEL_INFERENCE,
                        reference="events:validated-repair-decision",
                    ),
                    created_at=context.assembled_at,
                )
            )
        return replace(context, items=tuple(items))


def filter_repair_tools(context: ModelContext, names: frozenset[str]) -> frozenset[str]:
    item = next(
        (
            i
            for i in context.items
            if i.source.reference == POLICY_REFERENCE
            and i.trust is TrustClass.RUNTIME_POLICY
            and i.source.origin is TrustClass.RUNTIME_POLICY
        ),
        None,
    )
    if item is None:
        return names
    phase = json.loads(item.content)["phase"]
    if phase == "decision_required":
        return frozenset({DECISION_TOOL})
    if phase == "act" and json.loads(item.content).get("force_attempt") is True:
        return names & (EDIT_ACTIONS | {DECISION_TOOL})
    return names - INVESTIGATION if phase == "act" else names


def repair_tool_spec(context: ModelContext, spec: ModelToolSpec) -> ModelToolSpec:
    """Advertise only decision statuses accepted by the frozen protocol."""
    if spec.name != DECISION_TOOL:
        return spec
    policy = next(
        (
            i
            for i in context.items
            if i.source.reference == POLICY_REFERENCE
            and i.trust is TrustClass.RUNTIME_POLICY
            and i.source.origin is TrustClass.RUNTIME_POLICY
        ),
        None,
    )
    if policy is None:
        return spec
    data = json.loads(policy.content)
    if data["phase"] == "decision_required":
        return spec
    schema = json.loads(json.dumps(spec.parameters))
    if not data.get("revisable", False):
        schema["properties"]["status"]["enum"] = ["unable"]
        description = (
            "A decision already exists. Only record status unable with a concrete blocker."
        )
    else:
        description = (
            "Revise the existing hypothesis only when evidence changes it, or report inability. "
            "This does not refresh investigation allowance or grant permission."
        )
    return replace(spec, description=description, parameters=schema)


class RepairDecisionModel:
    def __init__(self, delegate: ModelPort, state: RepairDecisionState) -> None:
        self.delegate, self.state = delegate, state

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.delegate.capabilities

    def propose_action(self, context: ModelContext) -> ModelTurn:
        projection = self.state.project(self.state.store.events_for(context.run_id))
        if projection.phase == "unable":
            raise ModelTurnError(
                ModelFailureClass.PERMANENT,
                "CODING_REPAIR_UNABLE",
                "Local model recorded inability to continue; preserve its repair decision",
            )
        turn = self.delegate.propose_action(context)
        name = turn.action.tool_name
        violation = ""
        if projection.phase == "decision_required" and name != DECISION_TOOL:
            violation = "Missing decision: call record_repair_decision before work."
        if projection.phase == "act" and name in INVESTIGATION:
            violation = "Investigation allowance exhausted: attempt a source edit next."
        if (
            projection.phase == "act"
            and self.state.force_attempt
            and name not in EDIT_ACTIONS | {DECISION_TOOL}
        ):
            violation = "Best-guess attempt required: use apply_edits before more checks or reads."
        if name == DECISION_TOOL:
            try:
                decision = RepairDecision.model_validate(dict(turn.action.arguments))
                if projection.record is not None and decision == projection.record.decision:
                    violation = (
                        "Identical decision is not progress. Use the source already read to "
                        "revise the disproven hypothesis, perform a useful allowed action, "
                        "or report the concrete blocker with status unable."
                    )
                if (
                    projection.record is not None
                    and not self.state.revisable
                    and decision.status != "unable"
                ):
                    violation = (
                        "Decision already recorded: do not record it again; apply the proposed fix."
                    )
                if self.state.force_attempt and not decision.best_guess.strip():
                    violation = "Missing best_guess: state a concrete proposed source change."
            except ValidationError:
                violation = (
                    "Invalid decision schema: use only the advertised fields and status values."
                )
        if violation:
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT,
                "REPAIR_DECISION_REQUIRED",
                f"Rejected tool {name}: {violation} No action executed or allowance refreshed.",
                usage=turn.usage,
                context_admission=turn.context_admission,
            )
        return turn


class RepairDecisionTools:
    def __init__(
        self,
        delegate: ToolExecutorPort,
        state: RepairDecisionState,
        writable: Callable[[str], bool],
    ) -> None:
        self.delegate, self.state, self.writable = delegate, state, writable

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        if tool_name != DECISION_TOOL:
            return self.delegate.metadata_for(tool_name)
        return ToolMetadata(
            name=tool_name,
            risk=RiskLevel.READ_ONLY,
            required_permission=Permission.READ,
            side_effect=SideEffectClass.PURE,
            retry=RetryClass.NEVER,
            idempotency=IdempotencyClass.NOT_APPLICABLE,
            approval=ApprovalClass.NONE,
            timeout_seconds=5,
            sensitivity=DataSensitivity.INTERNAL,
        )

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        if request.proposal.tool_name != DECISION_TOOL:
            return self.delegate.execute(request)
        try:
            decision = RepairDecision.model_validate(dict(request.proposal.arguments))
            if self.state.force_attempt and not decision.best_guess.strip():
                msg = "A concrete best_guess is required"
                raise ValueError(msg)  # noqa: TRY301
            if not self.writable(decision.path):
                msg = "Decision target is outside the writable repair contract"
                raise ValueError(msg)  # noqa: TRY301 - normalize tool boundary denial
            active: tuple[Event, ...] | None = None
            for run in self.state.store.list_runs():
                events = self.state.store.events_for(run.run_id)
                current = replay(run.run_id, events)
                if (
                    current.current_action_id == request.proposal.action_id
                    and current.execution_in_flight
                ):
                    active = events
                    break
            if active is None:
                msg = "Decision requires a journaled current action"
                raise ValueError(msg)  # noqa: TRY301 - normalize tool boundary denial
            projection = self.state.project(active)
            if projection.record is not None and decision == projection.record.decision:
                msg = "Identical decision is not a revision"
                raise ValueError(msg)  # noqa: TRY301 - independent tool-boundary validation
            if (
                projection.record is not None
                and not self.state.revisable
                and decision.status != "unable"
            ):
                msg = "Rewording a decision cannot refresh investigation allowance"
                raise ValueError(msg)  # noqa: TRY301 - normalize tool boundary denial
            record = DecisionRecord(cycle_key=self.state.cycle_key(), decision=decision)
        except ValueError:
            return ToolResult(
                ok=False,
                observation="Repair decision denied: invalid schema, target, "
                "unjournaled action or repeated decision",
                error_code="REPAIR_DECISION_DENIED",
                failure_class=ToolFailureClass.PERMANENT,
            )
        return ToolResult(ok=True, observation=record.model_dump_json())
