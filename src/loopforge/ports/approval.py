from __future__ import annotations

from typing import Protocol

from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunId


class ApprovalGatewayPort(Protocol):
    """Operator approval boundary for approval-gated actions (PACS-014).

    The runtime is the only implementation: approval decisions enter the
    system exclusively as durable domain events on the run's authoritative
    stream, so a paused run survives process restarts and can be granted or
    rejected by any operator client wired to the same store. A grant can
    never expand authority — permissions are re-authorized before the
    approved action executes.
    """

    def grant_approval(self, run_id: RunId, action_id: ActionId) -> RunState:
        """Durably approve the pending action the run is waiting on."""
        ...

    def reject_approval(self, run_id: RunId, action_id: ActionId, *, reason: str) -> RunState:
        """Durably deny the pending action the run is waiting on."""
        ...
