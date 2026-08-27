from __future__ import annotations

from datetime import datetime

from loopforge.domain.context import ContextItem, ContextSource, ModelContext, ModelRole
from loopforge.domain.context_lifecycle import (
    ContextAccounting,
    ContextCandidate,
    ContextTokenBudget,
    PreservationClass,
    select_context,
)
from loopforge.domain.prompts import PromptTemplate
from loopforge.domain.security import TrustClass
from loopforge.domain.state import RunState
from loopforge.domain.tooling import DataSensitivity, SideEffectClass
from loopforge.domain.types import ContextItemId, RunStatus
from loopforge.ports.clock import ClockPort
from loopforge.ports.context import TokenCounterPort


class BasicContextBuilder:
    """Deterministic context assembly directly from the run projection.

    Trust classes are assigned by the runtime, never by content: the operator
    objective is authorized-human input, the runtime plan is runtime policy,
    and tool/verifier feedback is deterministic observation. This adapter
    performs no budgeting or compaction; use BudgetedContextBuilder when a
    token budget must be enforced.
    """

    def __init__(self, clock: ClockPort) -> None:
        self._clock = clock

    def build_context(
        self,
        state: RunState,
        *,
        role: ModelRole = ModelRole.CONTROLLER,
        token_budget: ContextTokenBudget | None = None,
    ) -> ModelContext:
        del token_budget  # budgeting is BudgetedContextBuilder's contract
        now = self._clock.now()
        items: list[ContextItem] = []

        def item(key: str, content: str, trust: TrustClass, detail: str) -> ContextItem:
            return ContextItem(
                item_id=ContextItemId(f"{state.run_id}:{key}"),
                content=content,
                trust=trust,
                source=ContextSource(
                    origin=trust,
                    reference=f"run:{state.run_id}:{key}",
                    detail=detail,
                ),
                sensitivity=DataSensitivity.INTERNAL,
                created_at=now,
            )

        if state.objective:
            items.append(
                item(
                    "objective",
                    state.objective,
                    TrustClass.AUTHORIZED_HUMAN,
                    "operator-supplied run objective",
                )
            )
        if state.plan:
            items.append(
                item(
                    "plan",
                    state.plan,
                    TrustClass.RUNTIME_POLICY,
                    "runtime-generated control plan",
                )
            )
        if state.last_observation is not None:
            items.append(
                item(
                    "observation",
                    state.last_observation,
                    TrustClass.DETERMINISTIC_OBSERVATION,
                    "latest journaled tool observation",
                )
            )
        if state.last_verification is not None:
            if state.last_verification_passed is None:
                outcome = "unknown"
            else:
                outcome = "passed" if state.last_verification_passed else "failed"
            items.append(
                item(
                    "verification",
                    state.last_verification,
                    TrustClass.DETERMINISTIC_OBSERVATION,
                    f"latest verifier outcome ({outcome})",
                )
            )

        return ModelContext(run_id=state.run_id, items=tuple(items), assembled_at=now, role=role)


class CharsPerTokenCounter:
    """Deterministic character-heuristic token counter.

    This is a provider-independent budgeting heuristic (default: 4 characters
    per token, rounded up), not any provider's tokenizer.
    """

    def __init__(self, chars_per_token: int = 4) -> None:
        if chars_per_token <= 0:
            msg = "chars_per_token must be positive"
            raise ValueError(msg)
        self._chars_per_token = chars_per_token

    def count_tokens(self, text: str) -> int:
        return (len(text) + self._chars_per_token - 1) // self._chars_per_token


class BudgetedContextBuilder:
    """Context assembly with deterministic selection, budgeting, and compaction.

    Trust classes are assigned by origin, never by content (the same rule as
    BasicContextBuilder). Preservation classes map to code-owned state
    sections: objective, blockers, latest verifier failures, externally
    confirmed facts, pending approvals, and irreversible actions are always
    kept whole or the build fails explicitly. Compaction may drop or truncate
    non-preserved content but never alters trust or provenance.

    The static template text is charged against the budget as overhead, so
    the rendered prompt and its context items together fit the budget.
    """

    def __init__(
        self,
        clock: ClockPort,
        counter: TokenCounterPort,
        *,
        template: PromptTemplate,
        token_budget: ContextTokenBudget,
    ) -> None:
        self._clock = clock
        self._counter = counter
        self._template = template
        self._default_budget = token_budget
        self._last_accounting: ContextAccounting | None = None

    @property
    def last_accounting(self) -> ContextAccounting | None:
        """The ledger from the most recent successful build; None after a failed build."""
        return self._last_accounting

    def build_context(
        self,
        state: RunState,
        *,
        role: ModelRole = ModelRole.CONTROLLER,
        token_budget: ContextTokenBudget | None = None,
    ) -> ModelContext:
        now = self._clock.now()
        budget = token_budget or self._default_budget
        # A failed build must not leave a stale ledger attributable to it.
        self._last_accounting = None
        selection = select_context(
            tuple(self._candidates(state, now)),
            budget=budget,
            role=role,
            count_tokens=self._counter.count_tokens,
            now=now,
            overhead_tokens=self._counter.count_tokens(self._template.static_text()),
        )
        self._last_accounting = selection.accounting
        return ModelContext(
            run_id=state.run_id,
            items=selection.items,
            assembled_at=now,
            role=role,
            prompt_template=self._template.reference(),
        )

    def _candidates(self, state: RunState, now: datetime) -> list[ContextCandidate]:
        candidates: list[ContextCandidate] = []

        def item(key: str, content: str, trust: TrustClass, detail: str) -> ContextItem:
            return ContextItem(
                item_id=ContextItemId(f"{state.run_id}:{key}"),
                content=content,
                trust=trust,
                source=ContextSource(
                    origin=trust,
                    reference=f"run:{state.run_id}:{key}",
                    detail=detail,
                ),
                sensitivity=DataSensitivity.INTERNAL,
                created_at=now,
            )

        if state.objective:
            candidates.append(
                ContextCandidate(
                    item=item(
                        "objective",
                        state.objective,
                        TrustClass.AUTHORIZED_HUMAN,
                        "operator-supplied run objective",
                    ),
                    preserved=frozenset({PreservationClass.OBJECTIVE}),
                    compactible=False,
                )
            )
        if state.plan:
            candidates.append(
                ContextCandidate(
                    item=item(
                        "plan",
                        state.plan,
                        TrustClass.RUNTIME_POLICY,
                        "runtime-generated control plan",
                    ),
                    roles=frozenset({ModelRole.CONTROLLER, ModelRole.PLANNER}),
                )
            )

        blockers: list[str] = []
        if state.open_circuit_tools:
            blockers.append("open circuits: " + ", ".join(state.open_circuit_tools))
        if state.last_tool_failure_class is not None:
            blockers.append(f"last tool failure: {state.last_tool_failure_class.value}")
        if blockers:
            candidates.append(
                ContextCandidate(
                    item=item(
                        "blockers",
                        "; ".join(blockers),
                        TrustClass.DETERMINISTIC_OBSERVATION,
                        "active execution blockers",
                    ),
                    preserved=frozenset({PreservationClass.BLOCKER}),
                    compactible=False,
                )
            )

        if state.last_verification is not None and state.last_verification_passed is False:
            candidates.append(
                ContextCandidate(
                    item=item(
                        "verifier-failure",
                        f"verification failed: {state.last_verification}",
                        TrustClass.DETERMINISTIC_OBSERVATION,
                        "latest verifier outcome (failed)",
                    ),
                    preserved=frozenset({PreservationClass.VERIFIER_FAILURE}),
                    compactible=False,
                )
            )
        elif state.last_verification is not None and state.last_verification_passed is True:
            candidates.append(
                ContextCandidate(
                    item=item(
                        "confirmed-fact",
                        f"verified: {state.last_verification}",
                        TrustClass.DETERMINISTIC_OBSERVATION,
                        "externally confirmed verifier fact",
                    ),
                    preserved=frozenset({PreservationClass.CONFIRMED_FACT}),
                    compactible=False,
                )
            )
        elif state.last_verification is not None:
            candidates.append(
                ContextCandidate(
                    item=item(
                        "verification",
                        state.last_verification,
                        TrustClass.DETERMINISTIC_OBSERVATION,
                        "latest verifier outcome (unknown)",
                    )
                )
            )

        if state.status is RunStatus.WAITING_FOR_APPROVAL:
            proposal = state.current_proposal
            pending = (
                f"pending approval: {proposal.tool_name} action {proposal.action_id}"
                if proposal is not None
                else "pending approval: awaiting human decision"
            )
            candidates.append(
                ContextCandidate(
                    item=item(
                        "pending-approval",
                        pending,
                        TrustClass.RUNTIME_POLICY,
                        "action awaiting human approval",
                    ),
                    preserved=frozenset({PreservationClass.PENDING_APPROVAL}),
                    compactible=False,
                )
            )

        metadata = state.current_tool_metadata
        if metadata is not None and metadata.side_effect is SideEffectClass.IRREVERSIBLE:
            candidates.append(
                ContextCandidate(
                    item=item(
                        "irreversible-action",
                        f"irreversible action in scope: {metadata.name}",
                        TrustClass.RUNTIME_POLICY,
                        "authorized action with irreversible side effects",
                    ),
                    preserved=frozenset({PreservationClass.IRREVERSIBLE_ACTION}),
                    compactible=False,
                )
            )

        if state.last_observation is not None:
            candidates.append(
                ContextCandidate(
                    item=item(
                        "observation",
                        state.last_observation,
                        TrustClass.DETERMINISTIC_OBSERVATION,
                        "latest journaled tool observation",
                    ),
                    roles=frozenset({ModelRole.CONTROLLER, ModelRole.REFLECTOR}),
                )
            )
        if state.last_reflection is not None:
            candidates.append(
                ContextCandidate(
                    item=item(
                        "reflection",
                        state.last_reflection,
                        TrustClass.MODEL_INFERENCE,
                        "latest model reflection (unverified)",
                    ),
                    roles=frozenset({ModelRole.CONTROLLER, ModelRole.PLANNER}),
                )
            )

        return candidates
