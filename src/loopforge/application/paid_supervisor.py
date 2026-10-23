"""Parent reservations happen before paid calls, independently of role allocations."""

import hashlib
import json
from dataclasses import replace

from loopforge.domain.goals import GoalState
from loopforge.ports.paid_budget import PaidBudgetPort
from loopforge.ports.supervisor import (
    SupervisorFailure,
    SupervisorPort,
    SupervisorRequest,
    SupervisorResponse,
)


class BudgetedSupervisor:
    def __init__(self, delegate: SupervisorPort, budget: PaidBudgetPort) -> None:
        self.delegate, self.budget = delegate, budget

    def prepare(self, state: GoalState) -> SupervisorRequest:
        request = self.delegate.prepare(state)
        trace: dict[str, object]
        try:
            trace = json.loads(request.trace)
        except ValueError:
            trace = {"delegate_trace": request.trace}
        trace["parent_sequence"] = state.sequence
        return replace(request, trace=json.dumps(trace))

    def decide(self, request: SupervisorRequest) -> SupervisorResponse:
        key = "supervisor:" + hashlib.sha256((request.body + request.trace).encode()).hexdigest()
        try:
            self.budget.reserve(key, "supervisor", request.maximum_microusd)
        except ValueError:
            msg = "Parent paid budget exhausted or call already reserved"
            raise SupervisorFailure(msg, 0) from None
        try:
            response = self.delegate.decide(request)
        except SupervisorFailure as exc:
            if exc.charged_microusd is not None:
                self.budget.settle(key, exc.charged_microusd)
            raise
        self.budget.settle(key, response.charged_microusd)
        return response
