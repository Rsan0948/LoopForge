"""Candidate-policy shadow advisor (PACS-017).

``CandidateShadowAdvisor`` evaluates one candidate ``ExecutionPolicy`` at the
active run's decision points and reports what the candidate WOULD have done:
routing through a candidate-configured ``RoutingPolicyPort``, context
budgeting through the candidate's ``ContextAllocationBounds`` over the same
accounting ledger the active builder just produced, and verification cadence
through the candidate's ``verify_read_only_turns`` knob applied to the same
code-owned ``ToolMetadata`` permission class (the model can never define or
downgrade it — AGENTS.md rule 4).

The advisor is pure and I/O-free; its advice is journaled as durable
evidence-only ``ShadowDecisionRecorded`` events and never enacted.
"""

from __future__ import annotations

from loopforge.domain.context_lifecycle import DropReason
from loopforge.domain.policies import ExecutionPolicy, ShadowDecisionKind
from loopforge.domain.state import RunState
from loopforge.domain.types import Permission
from loopforge.ports.context import ContextAccountingSource
from loopforge.ports.routing import RoutingPolicyPort, RoutingSignals
from loopforge.ports.shadow import ShadowAdvice


class CandidateShadowAdvisor:
    """Shadow advisor mirroring the active decision points with candidate knobs."""

    def __init__(
        self,
        policy: ExecutionPolicy,
        *,
        router: RoutingPolicyPort,
        accounting_source: object,
    ) -> None:
        self._policy = policy
        self._router = router
        self._accounting_source = accounting_source
        self._shadow_budget = policy.initial_context_budget()

    @property
    def policy(self) -> ExecutionPolicy:
        return self._policy

    def advise_route(self, state: RunState, *, signals: RoutingSignals) -> ShadowAdvice:
        decision = self._router.route(state, signals=signals)
        if decision.model is None:
            choice = "no-compatible-model"
        else:
            capabilities = decision.model.capabilities
            tier = decision.tier.value if decision.tier is not None else "none"
            choice = f"{capabilities.provider}/{capabilities.model} tier={tier}"
        return ShadowAdvice(
            kind=ShadowDecisionKind.MODEL_ROUTE,
            decision=choice,
            basis=f"reason_code={decision.reason_code.value}",
        )

    def advise_context_budget(self, state: RunState) -> ShadowAdvice:
        del state  # the ledger carries the adaptation signals, not the projection
        budget = self._shadow_budget
        accounting = (
            self._accounting_source.last_accounting
            if isinstance(self._accounting_source, ContextAccountingSource)
            else None
        )
        if accounting is None or accounting.usable_tokens <= 0:
            basis = "accounting=absent"
        else:
            dropped = any(
                entry.drop_reason is DropReason.OVER_BUDGET for entry in accounting.dropped_entries
            )
            utilization = accounting.used_tokens / accounting.usable_tokens
            basis = f"dropped_over_budget={dropped} utilization_fraction={utilization:.3f}"
            self._shadow_budget = self._policy.context_allocation.adjust(
                budget,
                dropped_over_budget=dropped,
                utilization_fraction=utilization,
            )
        return ShadowAdvice(
            kind=ShadowDecisionKind.CONTEXT_BUDGET,
            decision=f"max_tokens={budget.max_tokens} reserve_tokens={budget.reserve_tokens}",
            basis=basis,
        )

    def advise_verification_cadence(self, state: RunState) -> ShadowAdvice:
        metadata = state.current_tool_metadata
        read_only_turn = metadata is not None and metadata.required_permission is Permission.READ
        tool_failure = state.last_tool_failure_class is not None
        # The candidate's cadence knob applied to the same code-owned metadata
        # rule the active runtime uses (ADR-0013): only successful READ-only
        # turns may skip, and only when the candidate skips read-only turns.
        skip = not self._policy.verify_read_only_turns and not tool_failure and read_only_turn
        return ShadowAdvice(
            kind=ShadowDecisionKind.VERIFICATION_CADENCE,
            decision="skip" if skip else "verify",
            basis=(
                f"verify_read_only_turns={self._policy.verify_read_only_turns} "
                f"read_only_turn={read_only_turn} tool_failure={tool_failure}"
            ),
        )
