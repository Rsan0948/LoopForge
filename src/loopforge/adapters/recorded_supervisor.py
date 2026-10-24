"""Operator-side record of every supervisor prompt; decisions pass through unchanged."""

from pathlib import Path

from loopforge.domain.goals import GoalState
from loopforge.ports.supervisor import (
    EvidenceAwareSupervisorPort,
    EvidenceStorePort,
    SupervisorRequest,
    SupervisorResponse,
)


class RecordedSupervisor:
    """Writes each prepared request body once per goal sequence; a changed body is an error."""

    def __init__(self, delegate: EvidenceAwareSupervisorPort, directory: Path) -> None:
        self.delegate, self.directory = delegate, directory
        directory.mkdir(parents=True, exist_ok=True)

    def attach_evidence(self, store: EvidenceStorePort) -> None:
        self.delegate.attach_evidence(store)

    def prepare(self, state: GoalState) -> SupervisorRequest:
        request = self.delegate.prepare(state)
        path = self.directory / f"{state.sequence}.json"
        if path.exists() and path.read_text() != request.body:
            msg = "Recorded prompt changed"
            raise ValueError(msg)
        path.write_text(request.body)
        return request

    def decide(self, request: SupervisorRequest) -> SupervisorResponse:
        return self.delegate.decide(request)
