"""Pins for the derived provenance projection (PACS-015).

The acceptance gate: the event log stays authoritative and the graph is
exactly rebuildable from it; every material patch traces to triggering
evidence/actions and subsequent verification; the graph never fabricates
hidden chain-of-thought; reconstruction is deterministic for the same stream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

import pytest

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.application.provenance import build_provenance_graph, explain_provenance
from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import ArtifactKind
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    ApprovalGranted,
    ApprovalRequested,
    ArtifactRecorded,
    BudgetDebited,
    ContextAssembled,
    Event,
    ModelTurnRecorded,
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
from loopforge.domain.provenance import (
    ProvenanceEdgeKind,
    ProvenanceNodeKind,
    UnknownProvenanceNodeError,
)
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import (
    ActionId,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    StopReason,
    UsageDelta,
    WorkerId,
    WorkspaceId,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)
RUN = RunId("provenance-run")


def _event_id(sequence: int) -> EventId:
    return EventId(f"e{sequence}")


def _proposal(action_id: str, tool_name: str) -> ActionProposal:
    return ActionProposal(ActionId(action_id), tool_name, {})


def _metadata(name: str = "inspect") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _debit(sequence: int) -> BudgetDebited:
    return BudgetDebited(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        usage=UsageDelta(cost_usd=0.01, input_tokens=10, output_tokens=5),
    )


def _repair_stream() -> tuple[Event, ...]:
    """A two-turn approval-gated repair: read, fail, reflect, edit, pass."""
    return (
        RunStarted(
            event_id=_event_id(1),
            run_id=RUN,
            occurred_at=NOW,
            sequence=1,
            objective="repair the calculator",
        ),
        PlanCreated(
            event_id=_event_id(2),
            run_id=RUN,
            occurred_at=NOW,
            sequence=2,
            plan="inspect then patch",
        ),
        ContextAssembled(
            event_id=_event_id(3), run_id=RUN, occurred_at=NOW, sequence=3, context_items=()
        ),
        _debit(4),
        ModelTurnRecorded(
            event_id=_event_id(5),
            run_id=RUN,
            occurred_at=NOW,
            sequence=5,
            provider="ollama",
            model="devstral-small-2:latest",
            action_id=ActionId("a1"),
        ),
        ActionProposed(
            event_id=_event_id(6),
            run_id=RUN,
            occurred_at=NOW,
            sequence=6,
            proposal=_proposal("a1", "read_file"),
        ),
        ActionAuthorized(
            event_id=_event_id(7),
            run_id=RUN,
            occurred_at=NOW,
            sequence=7,
            proposal=_proposal("a1", "read_file"),
            tool_metadata=_metadata("read_file"),
        ),
        ToolExecutionStarted(
            event_id=_event_id(8),
            run_id=RUN,
            occurred_at=NOW,
            sequence=8,
            action_id=ActionId("a1"),
            attempt=1,
            idempotency_key=None,
        ),
        ToolSucceeded(
            event_id=_event_id(9),
            run_id=RUN,
            occurred_at=NOW,
            sequence=9,
            action_id=ActionId("a1"),
            observation="run_tests: 1 failed (test_add)",
            attempt=1,
        ),
        VerificationFailed(
            event_id=_event_id(10),
            run_id=RUN,
            occurred_at=NOW,
            sequence=10,
            summary="command:run_tests: failed (exit_code=1)",
            score=0.0,
        ),
        ReflectionRecorded(
            event_id=_event_id(11),
            run_id=RUN,
            occurred_at=NOW,
            sequence=11,
            reflection="narrow the diff to adder.py",
        ),
        ContextAssembled(
            event_id=_event_id(12), run_id=RUN, occurred_at=NOW, sequence=12, context_items=()
        ),
        _debit(13),
        ModelTurnRecorded(
            event_id=_event_id(14),
            run_id=RUN,
            occurred_at=NOW,
            sequence=14,
            provider="ollama",
            model="devstral-small-2:latest",
            action_id=ActionId("a2"),
        ),
        ActionProposed(
            event_id=_event_id(15),
            run_id=RUN,
            occurred_at=NOW,
            sequence=15,
            proposal=_proposal("a2", "edit_file"),
        ),
        ApprovalRequested(
            event_id=_event_id(16),
            run_id=RUN,
            occurred_at=NOW,
            sequence=16,
            action_id=ActionId("a2"),
            reason="approval-gated write",
        ),
        ApprovalGranted(
            event_id=_event_id(17),
            run_id=RUN,
            occurred_at=NOW,
            sequence=17,
            action_id=ActionId("a2"),
        ),
        ActionAuthorized(
            event_id=_event_id(18),
            run_id=RUN,
            occurred_at=NOW,
            sequence=18,
            proposal=_proposal("a2", "edit_file"),
            tool_metadata=_metadata("edit_file"),
        ),
        ToolExecutionStarted(
            event_id=_event_id(19),
            run_id=RUN,
            occurred_at=NOW,
            sequence=19,
            action_id=ActionId("a2"),
            attempt=1,
            idempotency_key="loopforge:prov:a2",
        ),
        ToolSucceeded(
            event_id=_event_id(20),
            run_id=RUN,
            occurred_at=NOW,
            sequence=20,
            action_id=ActionId("a2"),
            observation="edited adder.py",
            attempt=1,
        ),
        VerificationPassed(
            event_id=_event_id(21),
            run_id=RUN,
            occurred_at=NOW,
            sequence=21,
            summary="command:run_tests: passed (exit_code=0)",
        ),
        ArtifactRecorded(
            event_id=_event_id(22),
            run_id=RUN,
            occurred_at=NOW,
            sequence=22,
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="workspace:fixture",
            content=(
                "workspace_id=fixture\nbase_revision=abc123\n\ndiff --git a/adder.py b/adder.py"
            ),
        ),
        RunStopped(
            event_id=_event_id(23),
            run_id=RUN,
            occurred_at=NOW,
            sequence=23,
            reason=StopReason.SUCCESS_VERIFIED,
            summary="run completed",
        ),
    )


# --- Determinism and rebuildability (acceptance gate) ---------------------------


def test_build_is_deterministic_for_the_same_stream() -> None:
    events = _repair_stream()
    first = build_provenance_graph(events)
    second = build_provenance_graph(events)

    assert first == second
    assert first.nodes == second.nodes
    assert first.edges == second.edges


def test_rebuild_from_a_fresh_store_read_matches_the_in_memory_build() -> None:
    events = _repair_stream()
    store = InMemoryEventStore()
    for index, event in enumerate(events):
        store.append(event, expected_version=index)

    rebuilt = build_provenance_graph(store.events_for(RUN))

    assert rebuilt == build_provenance_graph(events)


# --- Totality and no-fabrication pins --------------------------------------------


def test_every_event_becomes_exactly_one_node() -> None:
    events = _repair_stream()
    graph = build_provenance_graph(events)

    assert len(graph.nodes) == len(events)
    assert [node.sequence for node in graph.nodes] == [event.sequence for event in events]
    assert len({node.node_id for node in graph.nodes}) == len(events)
    for node, event in zip(graph.nodes, events, strict=True):
        assert node.event_type == type(event).__name__
        assert node.node_id == f"{node.kind.value}:{event.sequence}"


def test_every_edge_endpoint_exists() -> None:
    graph = build_provenance_graph(_repair_stream())
    node_ids = {node.node_id for node in graph.nodes}

    assert graph.edges, "a realistic stream must produce causal edges"
    for edge in graph.edges:
        assert edge.source_id in node_ids
        assert edge.target_id in node_ids


def test_causal_edges_point_forward_in_stream_order() -> None:
    # Primary causal edges and the backbone always point from an earlier event
    # to a later one; associative edges (an approval OF an earlier action, an
    # instruction AMENDS the earlier requirement) legitimately point backward
    # to the node they annotate.
    graph = build_provenance_graph(_repair_stream())
    sequences = {node.node_id: node.sequence for node in graph.nodes}
    associative = {ProvenanceEdgeKind.APPROVAL_OF, ProvenanceEdgeKind.AMENDS}

    for edge in graph.edges:
        if edge.kind in associative:
            continue
        assert sequences[edge.source_id] < sequences[edge.target_id]


def test_node_vocabulary_contains_no_reasoning_kinds() -> None:
    for kind in ProvenanceNodeKind:
        assert "thought" not in kind.value
        assert "reasoning" not in kind.value


def test_backbone_links_every_consecutive_pair() -> None:
    events = _repair_stream()
    graph = build_provenance_graph(events)
    backbone = [edge for edge in graph.edges if edge.kind is ProvenanceEdgeKind.SEQUENCE]

    assert len(backbone) == len(events) - 1
    for edge, (before, after) in zip(backbone, pairwise(events), strict=True):
        assert edge.source_id.endswith(f":{before.sequence}")
        assert edge.target_id.endswith(f":{after.sequence}")


# --- Patch traceability (acceptance gate) -----------------------------------------


def test_explain_traces_the_patch_to_model_turn_action_and_verification() -> None:
    graph = build_provenance_graph(_repair_stream())

    explanation = explain_provenance(graph, "artifact:22")

    assert [node.node_id for node in explanation.chain] == [
        "requirement:1",
        "context:12",
        "model_turn:14",
        "action:15",
        "action:18",
        "tool_execution:19",
        "tool_result:20",
        "verification:21",
    ]
    # The model turn in the chain carries the durable identity of the model
    # that produced the patch's action.
    model_turn = explanation.chain[2]
    assert model_turn.attribute("provider") == "ollama"
    assert model_turn.attribute("model") == "devstral-small-2:latest"


def test_explain_supporting_holds_triggering_failure_evidence_and_approvals() -> None:
    graph = build_provenance_graph(_repair_stream())

    explanation = explain_provenance(graph, "artifact:22")
    supporting = {node.node_id for node in explanation.supporting}

    # Turn 1's failure evidence (the trigger for the patch's turn) and the
    # approval gate on the edit action are supporting context, not the spine.
    assert {
        "verification:10",
        "reflection:11",
        "model_turn:5",
        "approval:16",
        "approval:17",
    } <= supporting


def test_explain_action_outcomes_reach_verification_evidence_and_stop() -> None:
    graph = build_provenance_graph(_repair_stream())

    explanation = explain_provenance(graph, "action:15")
    outcomes = [node.node_id for node in explanation.outcomes]

    assert "verification:21" in outcomes
    assert "artifact:22" in outcomes
    assert "stop:23" in outcomes


def test_explain_outcomes_never_cascade_past_a_model_turn() -> None:
    graph = build_provenance_graph(_repair_stream())

    explanation = explain_provenance(graph, "verification:10")
    outcome_ids = [node.node_id for node in explanation.outcomes]

    # Turn 1's verification triggered the reflection and turn 2's model turn,
    # but turn 2's own actions are not "outcomes" of turn 1's verification.
    assert outcome_ids == ["reflection:11", "model_turn:14"]


def test_explain_rejects_an_unknown_node() -> None:
    graph = build_provenance_graph(_repair_stream())

    with pytest.raises(UnknownProvenanceNodeError, match="unknown provenance node"):
        explain_provenance(graph, "artifact:999")


# --- Absence tolerance: pre-PACS-015 streams have no model-turn records -----------


def test_stream_without_model_turn_records_still_builds() -> None:
    events = tuple(event for event in _repair_stream() if not isinstance(event, ModelTurnRecorded))

    graph = build_provenance_graph(events)

    assert not [node for node in graph.nodes if node.kind is ProvenanceNodeKind.MODEL_TURN]
    assert not [edge for edge in graph.edges if edge.kind is ProvenanceEdgeKind.PRODUCED]
    explanation = explain_provenance(graph, "artifact:22")
    # Attribution degrades honestly: the chain ends at the proposed action and
    # the approvals degrade to supporting evidence — nothing is invented.
    assert [node.node_id for node in explanation.chain] == [
        "action:15",
        "action:18",
        "tool_execution:19",
        "tool_result:20",
        "verification:21",
    ]
    assert {node.node_id for node in explanation.supporting} == {"approval:16", "approval:17"}


# --- Retry and reliability edges ---------------------------------------------------


def test_retry_chain_links_the_failure_retry_and_next_execution() -> None:
    events: tuple[Event, ...] = (
        RunStarted(
            event_id=_event_id(1), run_id=RUN, occurred_at=NOW, sequence=1, objective="repair"
        ),
        PlanCreated(event_id=_event_id(2), run_id=RUN, occurred_at=NOW, sequence=2, plan="inspect"),
        ContextAssembled(
            event_id=_event_id(3), run_id=RUN, occurred_at=NOW, sequence=3, context_items=()
        ),
        ActionProposed(
            event_id=_event_id(4),
            run_id=RUN,
            occurred_at=NOW,
            sequence=4,
            proposal=_proposal("a1", "run_tests"),
        ),
        ActionAuthorized(
            event_id=_event_id(5),
            run_id=RUN,
            occurred_at=NOW,
            sequence=5,
            proposal=_proposal("a1", "run_tests"),
            tool_metadata=_metadata("run_tests"),
        ),
        ToolExecutionStarted(
            event_id=_event_id(6),
            run_id=RUN,
            occurred_at=NOW,
            sequence=6,
            action_id=ActionId("a1"),
            attempt=1,
            idempotency_key=None,
        ),
        ToolFailed(
            event_id=_event_id(7),
            run_id=RUN,
            occurred_at=NOW,
            sequence=7,
            action_id=ActionId("a1"),
            error_code="TOOL_TIMEOUT",
            error_message="timed out",
            failure_class=ToolFailureClass.TRANSIENT,
            attempt=1,
        ),
        RetryScheduled(
            event_id=_event_id(8),
            run_id=RUN,
            occurred_at=NOW,
            sequence=8,
            action_id=ActionId("a1"),
            next_attempt=2,
            delay_seconds=0.5,
            reason_code="RETRY_TRANSIENT_FAILURE",
        ),
        ToolExecutionStarted(
            event_id=_event_id(9),
            run_id=RUN,
            occurred_at=NOW,
            sequence=9,
            action_id=ActionId("a1"),
            attempt=2,
            idempotency_key=None,
        ),
        ToolSucceeded(
            event_id=_event_id(10),
            run_id=RUN,
            occurred_at=NOW,
            sequence=10,
            action_id=ActionId("a1"),
            observation="all tests pass",
            attempt=2,
        ),
    )

    graph = build_provenance_graph(events)
    edge_set = {(edge.source_id, edge.target_id, edge.kind) for edge in graph.edges}

    assert ("tool_result:7", "retry:8", ProvenanceEdgeKind.RETRIED) in edge_set
    assert ("retry:8", "tool_execution:9", ProvenanceEdgeKind.RETRIED) in edge_set
    assert ("tool_execution:9", "tool_result:10", ProvenanceEdgeKind.RESULTED_IN) in edge_set


def test_legacy_stream_without_execution_journal_links_the_result_to_authorized() -> None:
    # Legacy schema-v1 streams may carry an outcome directly after authorized.
    events: tuple[Event, ...] = (
        RunStarted(
            event_id=_event_id(1), run_id=RUN, occurred_at=NOW, sequence=1, objective="repair"
        ),
        PlanCreated(event_id=_event_id(2), run_id=RUN, occurred_at=NOW, sequence=2, plan="inspect"),
        ContextAssembled(
            event_id=_event_id(3), run_id=RUN, occurred_at=NOW, sequence=3, context_items=()
        ),
        ActionProposed(
            event_id=_event_id(4),
            run_id=RUN,
            occurred_at=NOW,
            sequence=4,
            proposal=_proposal("a1", "run_tests"),
        ),
        ActionAuthorized(
            event_id=_event_id(5),
            run_id=RUN,
            occurred_at=NOW,
            sequence=5,
            proposal=_proposal("a1", "run_tests"),
            tool_metadata=_metadata("run_tests"),
        ),
        ToolSucceeded(
            event_id=_event_id(6),
            run_id=RUN,
            occurred_at=NOW,
            sequence=6,
            action_id=ActionId("a1"),
            observation="all tests pass",
            attempt=1,
        ),
    )

    graph = build_provenance_graph(events)
    edge_set = {(edge.source_id, edge.target_id, edge.kind) for edge in graph.edges}

    assert ("action:5", "tool_result:6", ProvenanceEdgeKind.RESULTED_IN) in edge_set


# --- Operator authority and worker lifecycle ---------------------------------------


def test_amending_instruction_edges_to_the_requirement() -> None:
    events: tuple[Event, ...] = (
        RunStarted(
            event_id=_event_id(1), run_id=RUN, occurred_at=NOW, sequence=1, objective="repair"
        ),
        PlanCreated(event_id=_event_id(2), run_id=RUN, occurred_at=NOW, sequence=2, plan="inspect"),
        OperatorInstruction(
            event_id=_event_id(3),
            run_id=RUN,
            occurred_at=NOW,
            sequence=3,
            instruction="focus on adder.py only",
            amends_objective=True,
        ),
    )

    graph = build_provenance_graph(events)
    edge_set = {(edge.source_id, edge.target_id, edge.kind) for edge in graph.edges}

    assert (
        "operator_instruction:3",
        "requirement:1",
        ProvenanceEdgeKind.AMENDS,
    ) in edge_set


def test_requirement_node_exposes_the_parent_run_link_for_lineage() -> None:
    events: tuple[Event, ...] = (
        RunStarted(
            event_id=_event_id(1),
            run_id=RUN,
            occurred_at=NOW,
            sequence=1,
            objective="continue the repair",
            parent_run_id=RunId("run-parent"),
        ),
    )

    graph = build_provenance_graph(events)

    assert graph.node("requirement:1").attribute("parent_run_id") == "run-parent"


def test_root_requirement_node_omits_the_parent_run_link() -> None:
    # Root runs carry no lineage fabrications: the attribute is absent, not empty.
    events: tuple[Event, ...] = (
        RunStarted(
            event_id=_event_id(1), run_id=RUN, occurred_at=NOW, sequence=1, objective="repair"
        ),
    )

    graph = build_provenance_graph(events)

    assert graph.node("requirement:1").attribute("parent_run_id") is None


def test_worker_lifecycle_nodes_correlate_in_order() -> None:
    events: tuple[Event, ...] = (
        RunStarted(
            event_id=_event_id(1), run_id=RUN, occurred_at=NOW, sequence=1, objective="repair"
        ),
        PlanCreated(
            event_id=_event_id(2), run_id=RUN, occurred_at=NOW, sequence=2, plan="delegate"
        ),
        WorkerSpawned(
            event_id=_event_id(3),
            run_id=RUN,
            occurred_at=NOW,
            sequence=3,
            worker_id=WorkerId("worker-adder"),
            worker_run_id=RunId("worker-run-1"),
            workspace_id=WorkspaceId("ws-adder"),
            objective="repair adder.py",
            budget_share_cost_usd=0.5,
        ),
        WorkerStopped(
            event_id=_event_id(4),
            run_id=RUN,
            occurred_at=NOW,
            sequence=4,
            worker_id=WorkerId("worker-adder"),
            outcome=WorkerOutcome.SUCCEEDED,
            summary="worker finished",
        ),
        WorkerMerged(
            event_id=_event_id(5),
            run_id=RUN,
            occurred_at=NOW,
            sequence=5,
            worker_id=WorkerId("worker-adder"),
            outcome=MergeOutcome.MERGED,
            revision="a" * 40,
            detail="merged worker branch",
        ),
    )

    graph = build_provenance_graph(events)
    edge_set = {(edge.source_id, edge.target_id, edge.kind) for edge in graph.edges}

    assert ("worker:3", "worker:4", ProvenanceEdgeKind.CORRELATES) in edge_set
    assert ("worker:4", "worker:5", ProvenanceEdgeKind.CORRELATES) in edge_set
    spawned = graph.node("worker:3")
    assert spawned.attribute("worker_run_id") == "worker-run-1"


# --- Summary hygiene -----------------------------------------------------------------


def test_node_summaries_are_truncated_and_flattened() -> None:
    long_observation = "line one\nline two " + "x" * 200
    events: tuple[Event, ...] = (
        RunStarted(
            event_id=_event_id(1), run_id=RUN, occurred_at=NOW, sequence=1, objective="repair"
        ),
        ToolSucceeded(
            event_id=_event_id(2),
            run_id=RUN,
            occurred_at=NOW,
            sequence=2,
            action_id=ActionId("a1"),
            observation=long_observation,
            attempt=1,
        ),
    )

    graph = build_provenance_graph(events)
    result = graph.node("tool_result:2")

    assert len(result.summary) <= 120
    assert "\n" not in result.summary
