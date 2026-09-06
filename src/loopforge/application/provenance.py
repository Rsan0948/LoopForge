"""Derived execution-provenance projection (PACS-015).

``build_provenance_graph`` is a pure, deterministic function of one run's
authoritative event stream: one node per durable event, a ``SEQUENCE``
backbone edge between consecutive events, and typed causal edges derived
from code-owned correlation identifiers — action lifecycles by ``action_id``,
turn structure from ``ContextAssembled``/``ModelTurnRecorded``, and
verification/evidence adjacency. Nothing is inferred from model output and
no hidden chain-of-thought is fabricated: absent events (pre-PACS-015
streams without ``ModelTurnRecorded``, turns whose action never executed)
simply yield absent nodes and edges.

``explain_provenance`` answers "why did this node happen?": a primary causal
spine walked back through typed edges, every other typed-edge ancestor as
supporting evidence, and forward outcomes (sequent verification, recorded
evidence, stop) that never cascade past a later model turn.
"""

from __future__ import annotations

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
from loopforge.domain.provenance import (
    ProvenanceEdge,
    ProvenanceEdgeKind,
    ProvenanceExplanation,
    ProvenanceGraph,
    ProvenanceNode,
    ProvenanceNodeKind,
)
from loopforge.domain.types import StopReason

_MAX_SUMMARY = 120

# Edge kinds that constitute primary causation for the explain spine.
# Associative evidence (approvals, amendments, worker correlations) is always
# reported as supporting context, never as the primary cause.
_SPINE_KINDS: frozenset[ProvenanceEdgeKind] = frozenset(
    {
        ProvenanceEdgeKind.INFORMED_BY,
        ProvenanceEdgeKind.TRIGGERED,
        ProvenanceEdgeKind.PRODUCED,
        ProvenanceEdgeKind.AUTHORIZED,
        ProvenanceEdgeKind.REJECTED,
        ProvenanceEdgeKind.EXECUTED,
        ProvenanceEdgeKind.RESULTED_IN,
        ProvenanceEdgeKind.RETRIED,
        ProvenanceEdgeKind.VERIFIED_BY,
        ProvenanceEdgeKind.RECORDED,
    }
)


def _truncate(text: str, limit: int = _MAX_SUMMARY) -> str:
    flat = " ".join(text.split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def _attributes(**pairs: object) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, str(value)) for key, value in pairs.items() if value is not None))


def _node_for(event: Event) -> ProvenanceNode:  # noqa: PLR0912, PLR0915 - the flat match dispatch is deliberate: each event projection stays a single auditable case, mirroring reduce_event
    """Project one durable event into exactly one provenance node."""
    kind: ProvenanceNodeKind
    summary: str
    attributes: tuple[tuple[str, str], ...] = ()
    match event:
        case RunStarted(objective=objective, parent_run_id=parent_run_id):
            kind = ProvenanceNodeKind.REQUIREMENT
            summary = _truncate(objective)
            attributes = _attributes(
                parent_run_id=str(parent_run_id) if parent_run_id is not None else None
            )
        case PlanCreated(plan=plan):
            kind = ProvenanceNodeKind.PLAN
            summary = _truncate(plan)
        case ContextAssembled(
            context_items=items,
            prompt_template_id=template_id,
            prompt_template_version=template_version,
        ):
            kind = ProvenanceNodeKind.CONTEXT
            summary = f"{len(items)} context item(s)"
            attributes = _attributes(
                item_count=len(items),
                prompt_template_id=template_id,
                prompt_template_version=template_version,
            )
        case ModelTurnRecorded(provider=provider, model=model, action_id=action_id):
            kind = ProvenanceNodeKind.MODEL_TURN
            summary = f"{provider}/{model}"
            attributes = _attributes(provider=provider, model=model, action_id=str(action_id))
        case ShadowDecisionRecorded(
            policy_id=policy_id,
            policy_version=policy_version,
            kind=shadow_kind,
            decision=decision,
        ):
            kind = ProvenanceNodeKind.SHADOW_DECISION
            summary = f"shadow {shadow_kind.value}: {_truncate(decision, 80)}"
            attributes = _attributes(
                policy_id=policy_id,
                policy_version=policy_version,
                shadow_kind=shadow_kind.value,
            )
        case ActionProposed(proposal=proposal):
            kind = ProvenanceNodeKind.ACTION
            summary = f"proposed {proposal.tool_name}"
            attributes = _attributes(
                action_id=str(proposal.action_id), tool_name=proposal.tool_name, stage="proposed"
            )
        case ActionAuthorized(proposal=proposal, tool_metadata=metadata):
            kind = ProvenanceNodeKind.ACTION
            summary = f"authorized {proposal.tool_name}"
            attributes = _attributes(
                action_id=str(proposal.action_id),
                tool_name=proposal.tool_name,
                stage="authorized",
                risk=metadata.risk.value,
            )
        case ActionRejected(proposal=proposal, reason_code=reason_code):
            kind = ProvenanceNodeKind.ACTION
            summary = f"rejected {proposal.tool_name}: {reason_code}"
            attributes = _attributes(
                action_id=str(proposal.action_id),
                tool_name=proposal.tool_name,
                stage="rejected",
                reason_code=reason_code,
            )
        case ToolExecutionStarted(action_id=action_id, attempt=attempt):
            kind = ProvenanceNodeKind.TOOL_EXECUTION
            summary = f"execution attempt {attempt}"
            attributes = _attributes(action_id=str(action_id), attempt=attempt)
        case ToolSucceeded(action_id=action_id, observation=observation, attempt=attempt):
            kind = ProvenanceNodeKind.TOOL_RESULT
            summary = _truncate(observation)
            attributes = _attributes(action_id=str(action_id), attempt=attempt, outcome="ok")
        case ToolFailed(
            action_id=action_id,
            error_code=error_code,
            error_message=error_message,
            failure_class=failure_class,
            attempt=attempt,
        ):
            kind = ProvenanceNodeKind.TOOL_RESULT
            summary = f"{error_code}: {_truncate(error_message, 80)}"
            attributes = _attributes(
                action_id=str(action_id),
                attempt=attempt,
                outcome="failed",
                error_code=error_code,
                failure_class=failure_class.value,
            )
        case RetryScheduled(action_id=action_id, next_attempt=next_attempt, reason_code=reason):
            kind = ProvenanceNodeKind.RETRY
            summary = f"retry to attempt {next_attempt} ({reason})"
            attributes = _attributes(action_id=str(action_id), next_attempt=next_attempt)
        case CircuitOpened(tool_name=tool_name, reason_code=reason_code):
            kind = ProvenanceNodeKind.RELIABILITY
            summary = f"circuit opened for {tool_name}: {reason_code}"
            attributes = _attributes(tool_name=tool_name, reason_code=reason_code)
        case VerificationPassed(summary=text):
            kind = ProvenanceNodeKind.VERIFICATION
            summary = _truncate(text)
            attributes = _attributes(outcome="passed")
        case VerificationFailed(summary=text, score=score):
            kind = ProvenanceNodeKind.VERIFICATION
            summary = _truncate(text)
            attributes = _attributes(
                outcome="failed", score=f"{score:.2f}" if score is not None else None
            )
        case ReflectionRecorded(reflection=reflection):
            kind = ProvenanceNodeKind.REFLECTION
            summary = _truncate(reflection)
        case ArtifactRecorded(kind=artifact_kind, label=label, content=content):
            kind = ProvenanceNodeKind.ARTIFACT
            summary = f"{artifact_kind.value}: {label}"
            attributes = _attributes(
                artifact_kind=artifact_kind.value,
                label=label,
                content_bytes=len(content.encode("utf-8")),
            )
        case BudgetDebited(usage=usage):
            kind = ProvenanceNodeKind.BUDGET
            summary = f"${usage.cost_usd:.4f} · {usage.input_tokens} in / {usage.output_tokens} out"
            attributes = _attributes(cost_usd=f"{usage.cost_usd:.6f}")
        case ApprovalRequested(action_id=action_id, reason=reason):
            kind = ProvenanceNodeKind.APPROVAL
            summary = f"approval requested: {_truncate(reason, 80)}"
            attributes = _attributes(action_id=str(action_id), stage="requested")
        case ApprovalGranted(action_id=action_id):
            kind = ProvenanceNodeKind.APPROVAL
            summary = "approval granted"
            attributes = _attributes(action_id=str(action_id), stage="granted")
        case ApprovalRejected(action_id=action_id, reason=reason):
            kind = ProvenanceNodeKind.APPROVAL
            summary = f"approval rejected: {_truncate(reason, 80)}"
            attributes = _attributes(action_id=str(action_id), stage="rejected")
        case OperatorInstruction(instruction=instruction, amends_objective=amends):
            kind = ProvenanceNodeKind.OPERATOR_INSTRUCTION
            suffix = " (amends objective)" if amends else ""
            summary = f"{_truncate(instruction, 100)}{suffix}"
            attributes = _attributes(amends_objective=amends)
        case RunStopped(reason=reason, summary=text):
            kind = ProvenanceNodeKind.STOP
            summary = f"{reason.value}: {_truncate(text, 90)}"
            attributes = _attributes(reason=reason.value)
        case WorkerSpawned(worker_id=worker_id, worker_run_id=worker_run_id):
            kind = ProvenanceNodeKind.WORKER
            summary = f"worker spawned: {worker_id}"
            attributes = _attributes(
                worker_id=str(worker_id), worker_run_id=str(worker_run_id), stage="spawned"
            )
        case WorkerStopped(worker_id=worker_id, outcome=outcome):
            kind = ProvenanceNodeKind.WORKER
            summary = f"worker {worker_id}: {outcome.value}"
            attributes = _attributes(worker_id=str(worker_id), stage="stopped")
        case WorkerMerged(worker_id=worker_id, outcome=outcome, revision=revision):
            kind = ProvenanceNodeKind.WORKER
            summary = f"worker {worker_id} merge: {outcome.value}"
            attributes = _attributes(worker_id=str(worker_id), stage="merged", revision=revision)
    return ProvenanceNode(
        node_id=f"{kind.value}:{event.sequence}",
        kind=kind,
        event_type=type(event).__name__,
        sequence=event.sequence,
        occurred_at=event.occurred_at.isoformat(),
        summary=summary,
        attributes=attributes,
    )


def build_provenance_graph(events: tuple[Event, ...]) -> ProvenanceGraph:  # noqa: PLR0912, PLR0915 - one flat pass over the stream keeps every event's edge derivation adjacent and auditable
    """Derive the provenance DAG for one run's authoritative event stream.

    Pure and deterministic: the same event tuple always yields an identical
    graph, so the projection is exactly rebuildable from the event log.
    """
    nodes: list[ProvenanceNode] = []
    edges: list[ProvenanceEdge] = []
    sequence_of: dict[str, int] = {}

    previous_id: str | None = None
    requirement_id: str | None = None
    last_context_id: str | None = None
    last_model_turn: tuple[str, str] | None = None  # (node id, action id)
    # Sticky pointer for evidence edges (reflection/artifact/stop attribution).
    last_verification_id: str | None = None
    # Consumable trigger attribution: a verification or reflection triggers
    # exactly the NEXT model turn and is then spent — re-attributing it to a
    # later turn (after an approval rejection, an operator instruction, or a
    # mid-loop tool result) would fabricate a causal claim the stream
    # contradicts. Spent turns fall back to the requirement (weak but honest).
    pending_trigger_id: str | None = None
    last_tool_result_id: str | None = None
    proposed_of: dict[str, str] = {}
    authorized_of: dict[str, str] = {}
    execution_of: dict[str, str] = {}
    result_of: dict[str, str] = {}
    pending_retry_of: dict[str, str] = {}
    last_worker_node_of: dict[str, str] = {}

    def link(source: str | None, target: str | None, kind: ProvenanceEdgeKind) -> None:
        if source is not None and target is not None and source != target:
            edges.append(ProvenanceEdge(source_id=source, target_id=target, kind=kind))

    for event in events:
        node = _node_for(event)
        nodes.append(node)
        sequence_of[node.node_id] = node.sequence
        link(previous_id, node.node_id, ProvenanceEdgeKind.SEQUENCE)
        previous_id = node.node_id

        match event:
            case RunStarted():
                requirement_id = node.node_id
            case PlanCreated():
                link(requirement_id, node.node_id, ProvenanceEdgeKind.INFORMED_BY)
            case ContextAssembled():
                link(requirement_id, node.node_id, ProvenanceEdgeKind.INFORMED_BY)
                last_context_id = node.node_id
            case ModelTurnRecorded(action_id=action_id):
                link(last_context_id, node.node_id, ProvenanceEdgeKind.INFORMED_BY)
                # The turn was triggered by the most recent unconsumed durable
                # signal: a reflection, a verification, or the requirement.
                trigger = pending_trigger_id or requirement_id
                link(trigger, node.node_id, ProvenanceEdgeKind.TRIGGERED)
                pending_trigger_id = None
                last_model_turn = (node.node_id, str(action_id))
            case ActionProposed(proposal=proposal):
                if last_model_turn is not None and last_model_turn[1] == str(proposal.action_id):
                    link(last_model_turn[0], node.node_id, ProvenanceEdgeKind.PRODUCED)
                proposed_of[str(proposal.action_id)] = node.node_id
            case ActionAuthorized(proposal=proposal):
                link(
                    proposed_of.get(str(proposal.action_id)),
                    node.node_id,
                    ProvenanceEdgeKind.AUTHORIZED,
                )
                authorized_of[str(proposal.action_id)] = node.node_id
            case ActionRejected(proposal=proposal):
                link(
                    proposed_of.get(str(proposal.action_id)),
                    node.node_id,
                    ProvenanceEdgeKind.REJECTED,
                )
            case ToolExecutionStarted(action_id=action_id):
                key = str(action_id)
                base = authorized_of.get(key) or proposed_of.get(key)
                link(base, node.node_id, ProvenanceEdgeKind.EXECUTED)
                retry = pending_retry_of.pop(key, None)
                link(retry, node.node_id, ProvenanceEdgeKind.RETRIED)
                execution_of[key] = node.node_id
            case ToolSucceeded(action_id=action_id) | ToolFailed(action_id=action_id):
                key = str(action_id)
                source = execution_of.get(key) or authorized_of.get(key) or proposed_of.get(key)
                link(source, node.node_id, ProvenanceEdgeKind.RESULTED_IN)
                result_of[key] = node.node_id
                last_tool_result_id = node.node_id
            case RetryScheduled(action_id=action_id):
                key = str(action_id)
                link(result_of.get(key), node.node_id, ProvenanceEdgeKind.RETRIED)
                pending_retry_of[key] = node.node_id
            case CircuitOpened():
                link(last_tool_result_id, node.node_id, ProvenanceEdgeKind.RESULTED_IN)
            case VerificationPassed() | VerificationFailed():
                link(last_tool_result_id, node.node_id, ProvenanceEdgeKind.VERIFIED_BY)
                last_tool_result_id = None
                last_verification_id = node.node_id
                pending_trigger_id = node.node_id
            case ReflectionRecorded():
                link(last_verification_id, node.node_id, ProvenanceEdgeKind.TRIGGERED)
                pending_trigger_id = node.node_id
            case ArtifactRecorded():
                link(last_verification_id, node.node_id, ProvenanceEdgeKind.RECORDED)
            case (
                ApprovalRequested(action_id=action_id)
                | ApprovalGranted(action_id=action_id)
                | ApprovalRejected(action_id=action_id)
            ):
                link(node.node_id, proposed_of.get(str(action_id)), ProvenanceEdgeKind.APPROVAL_OF)
            case OperatorInstruction(amends_objective=amends):
                if amends:
                    link(node.node_id, requirement_id, ProvenanceEdgeKind.AMENDS)
            case RunStopped(reason=reason):
                # Only a verified-success stop actually RESULTED from a
                # verification; cancelled/budget/failure stops have their
                # proximate cause in the reason payload, not in the last
                # verification (an edge there would be fabricated).
                if reason is StopReason.SUCCESS_VERIFIED:
                    link(last_verification_id, node.node_id, ProvenanceEdgeKind.RESULTED_IN)
            case WorkerSpawned(worker_id=worker_id):
                last_worker_node_of[str(worker_id)] = node.node_id
            case WorkerStopped(worker_id=worker_id) | WorkerMerged(worker_id=worker_id):
                key = str(worker_id)
                link(last_worker_node_of.get(key), node.node_id, ProvenanceEdgeKind.CORRELATES)
                last_worker_node_of[key] = node.node_id
            case BudgetDebited():
                pass  # Backbone-only evidence: cost attribution stays in the stream.
            case ShadowDecisionRecorded():
                # Backbone-only evidence (PACS-017): shadow advice is never
                # enacted, so no causal edge to the active run's decisions is
                # honest — the SEQUENCE backbone is the whole story.
                pass

    ordered_edges = sorted(
        edges,
        key=lambda edge: (
            sequence_of[edge.source_id],
            sequence_of[edge.target_id],
            edge.kind.value,
        ),
    )
    return ProvenanceGraph(nodes=tuple(nodes), edges=tuple(ordered_edges))


def explain_provenance(graph: ProvenanceGraph, node_id: str) -> ProvenanceExplanation:
    """Answer "why did this node happen?" from the derived graph alone."""
    node = graph.node(node_id)
    typed_inbound: dict[str, list[ProvenanceEdge]] = {}
    typed_outbound: dict[str, list[ProvenanceEdge]] = {}
    for edge in graph.edges:
        if edge.kind is ProvenanceEdgeKind.SEQUENCE:
            continue
        typed_inbound.setdefault(edge.target_id, []).append(edge)
        typed_outbound.setdefault(edge.source_id, []).append(edge)

    # All typed-edge ancestors (supporting evidence), gathered breadth-first
    # in deterministic edge order.
    ancestors: dict[str, None] = {}
    queue = [node.node_id]
    while queue:
        current = queue.pop(0)
        for edge in typed_inbound.get(current, []):
            if edge.source_id not in ancestors and edge.source_id != node.node_id:
                ancestors[edge.source_id] = None
                queue.append(edge.source_id)

    # The primary causal spine: repeatedly follow the most recent primary
    # cause (associative evidence never enters the spine).
    spine: list[str] = []
    current = node.node_id
    while True:
        inbound = [edge for edge in typed_inbound.get(current, []) if edge.kind in _SPINE_KINDS]
        if not inbound:
            break
        cause = max(
            inbound, key=lambda edge: (graph.node(edge.source_id).sequence, edge.kind.value)
        )
        if cause.source_id in spine:
            break  # defensive: the DAG cannot cycle, but never loop regardless
        spine.append(cause.source_id)
        current = cause.source_id
    spine.reverse()

    # Forward outcomes: typed descendants, never cascading past a later model
    # turn (a verification that triggered turn N+1 lists that turn as an
    # outcome without absorbing turn N+1's own downstream as "outcomes").
    outcomes: dict[str, None] = {}
    queue = [node.node_id]
    while queue:
        current = queue.pop(0)
        for edge in typed_outbound.get(current, []):
            if edge.target_id in outcomes or edge.target_id == node.node_id:
                continue
            outcomes[edge.target_id] = None
            if graph.node(edge.target_id).kind is not ProvenanceNodeKind.MODEL_TURN:
                queue.append(edge.target_id)

    spine_ids = set(spine)
    ordered = sorted(ancestors, key=lambda nid: graph.node(nid).sequence)
    ordered_outcomes = sorted(outcomes, key=lambda nid: graph.node(nid).sequence)
    return ProvenanceExplanation(
        node=node,
        chain=tuple(graph.node(nid) for nid in spine),
        supporting=tuple(graph.node(nid) for nid in ordered if nid not in spine_ids),
        outcomes=tuple(graph.node(nid) for nid in ordered_outcomes),
    )
