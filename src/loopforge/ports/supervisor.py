"""Provider-independent decision, prepared worker, and durable journal boundaries."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from loopforge.domain.goals import (
    GoalAction,
    GoalEvent,
    GoalState,
    SupervisorDecision,
    WorkerObservation,
)
from loopforge.domain.investigation import FailureKind, ResearchRecord


@dataclass(frozen=True)
class SupervisorRequest:
    body: str
    maximum_microusd: int
    trace: str


@dataclass(frozen=True)
class SupervisorResponse:
    decision: SupervisorDecision
    charged_microusd: int
    trace: str


class SupervisorFailure(Exception):
    """Safe diagnostic only; absent usage means charge the reserved maximum."""

    def __init__(self, reason: str, charged_microusd: int | None = None) -> None:
        super().__init__(reason)
        self.charged_microusd = charged_microusd


class SupervisorPort(Protocol):
    def prepare(self, state: GoalState) -> SupervisorRequest: ...
    def decide(self, request: SupervisorRequest) -> SupervisorResponse: ...


class EvidenceStorePort(Protocol):
    """Original evidence by identity, and worker artifacts too large to inline."""

    def original(self, record_id: str) -> str: ...
    def publish_worker(self, raw: str, revision: str, candidate: str) -> ResearchRecord: ...


@runtime_checkable
class EvidenceAwareSupervisorPort(SupervisorPort, Protocol):
    """A supervisor that reads original evidence. Decorators must forward attachment,
    otherwise the provider silently falls back to bounded excerpts."""

    def attach_evidence(self, store: EvidenceStorePort) -> None: ...


class GoalJournalPort(Protocol):
    def load(self, goal: str) -> GoalState: ...
    def append(self, goal: str, sequence: int, event: GoalEvent) -> GoalState: ...


class PreparedWorkerPort(Protocol):
    def observe(self) -> WorkerObservation: ...
    def reconcile(self) -> WorkerObservation: ...
    def execute(self, action: GoalAction, key: str) -> WorkerObservation: ...


class InvestigationFailure(ValueError):
    """Code-owned safe action diagnostics, distinct from provider transport failures."""

    def __init__(self, category: FailureKind, summary: str) -> None:
        super().__init__(summary)
        self.category = category


class InvestigationPort(Protocol):
    def perform(
        self, decision: SupervisorDecision, key: str, state: GoalState
    ) -> ResearchRecord: ...


@runtime_checkable
class ManagedGoalWorkerPort(PreparedWorkerPort, Protocol):
    def recover_interrupted(self) -> None: ...
