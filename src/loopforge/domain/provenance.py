"""Closed provenance vocabulary and immutable graph dataclasses (PACS-015).

Provenance is a DERIVED projection of the authoritative event stream: every
node is exactly one durable event and every edge is derived deterministically
from sequence order and code-owned correlation identifiers (action lifecycles
by ``action_id``, turn structure, verification/evidence adjacency). The node
vocabulary deliberately contains no reasoning/chain-of-thought kinds — the
graph records what observably happened, never what a model "thought".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProvenanceNodeKind(StrEnum):
    """Closed, code-owned vocabulary of provenance node kinds."""

    REQUIREMENT = "requirement"
    PLAN = "plan"
    CONTEXT = "context"
    MODEL_TURN = "model_turn"
    ACTION = "action"
    TOOL_EXECUTION = "tool_execution"
    TOOL_RESULT = "tool_result"
    RETRY = "retry"
    RELIABILITY = "reliability"
    VERIFICATION = "verification"
    REFLECTION = "reflection"
    ARTIFACT = "artifact"
    BUDGET = "budget"
    APPROVAL = "approval"
    OPERATOR_INSTRUCTION = "operator_instruction"
    WORKER = "worker"
    STOP = "stop"


class ProvenanceEdgeKind(StrEnum):
    """Closed, code-owned vocabulary of provenance edge kinds.

    Edges point from cause to effect; ``SEQUENCE`` is the trajectory backbone
    (immediate stream predecessor) and every other kind is a typed causal or
    correlation relationship derived from the durable payloads.
    """

    SEQUENCE = "sequence"
    INFORMED_BY = "informed_by"
    TRIGGERED = "triggered"
    PRODUCED = "produced"
    AUTHORIZED = "authorized"
    REJECTED = "rejected"
    EXECUTED = "executed"
    RESULTED_IN = "resulted_in"
    RETRIED = "retried"
    APPROVAL_OF = "approval_of"
    VERIFIED_BY = "verified_by"
    RECORDED = "recorded"
    AMENDS = "amends"
    CORRELATES = "correlates"


class UnknownProvenanceNodeError(LookupError):
    """Raised when a provenance query names a node the graph does not contain."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvenanceNode:
    """One durable event projected as a provenance node.

    ``attributes`` is a sorted tuple of string pairs (not a mapping) so the
    node stays frozen and hashable; payloads are truncated summaries only —
    full evidence content stays in the authoritative event stream.
    """

    node_id: str
    kind: ProvenanceNodeKind
    event_type: str
    sequence: int
    occurred_at: str
    summary: str
    attributes: tuple[tuple[str, str], ...] = ()

    def attribute(self, key: str) -> str | None:
        for name, value in self.attributes:
            if name == key:
                return value
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvenanceEdge:
    """A typed cause → effect link between two provenance nodes."""

    source_id: str
    target_id: str
    kind: ProvenanceEdgeKind


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvenanceGraph:
    """Immutable derived provenance DAG for one run's event stream.

    Nodes are in stream-sequence order; edges are sorted by (source sequence,
    target sequence, kind) at build time so the same event stream always
    yields an identical graph.
    """

    nodes: tuple[ProvenanceNode, ...]
    edges: tuple[ProvenanceEdge, ...]

    def node(self, node_id: str) -> ProvenanceNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        msg = f"unknown provenance node: {node_id!r}"
        raise UnknownProvenanceNodeError(msg)

    def node_by_sequence(self, sequence: int) -> ProvenanceNode:
        for node in self.nodes:
            if node.sequence == sequence:
                return node
        msg = f"no provenance node for event sequence {sequence}"
        raise UnknownProvenanceNodeError(msg)

    def inbound(self, node_id: str) -> tuple[ProvenanceEdge, ...]:
        return tuple(edge for edge in self.edges if edge.target_id == node_id)

    def outbound(self, node_id: str) -> tuple[ProvenanceEdge, ...]:
        return tuple(edge for edge in self.edges if edge.source_id == node_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvenanceExplanation:
    """The answer to "why did this node happen?" for one graph node.

    ``chain`` is the primary causal spine (oldest → newest, excluding the
    explained node); ``supporting`` holds every other typed-edge ancestor in
    sequence order; ``outcomes`` holds forward causal descendants (sequent
    verification, evidence, stop) without cascading past a later model turn.
    """

    node: ProvenanceNode
    chain: tuple[ProvenanceNode, ...]
    supporting: tuple[ProvenanceNode, ...]
    outcomes: tuple[ProvenanceNode, ...]
