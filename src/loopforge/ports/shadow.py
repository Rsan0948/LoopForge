"""Evidence-only shadow-policy port (PACS-017).

A shadowed candidate policy is consulted at the same decision points where
the active policy decides — model routing, context budgeting, and
verification cadence. Its advice is journaled as durable
``ShadowDecisionRecorded`` evidence and NEVER enacted: the active run's
decisions are byte-identical with or without a shadow wired (AGENTS.md
rule 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.policies import ExecutionPolicy, ShadowDecisionKind
from loopforge.domain.state import RunState
from loopforge.ports.routing import RoutingSignals


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadowAdvice:
    """One candidate-policy decision offered as evidence, never enacted.

    ``decision`` names the choice the candidate would have made and ``basis``
    the code-owned signals it derived from; both are bounded, sanitized text
    validated by the durable event's constructor.
    """

    kind: ShadowDecisionKind
    decision: str
    basis: str


class ShadowPolicyPort(Protocol):
    """Evidence-only candidate-policy advisor.

    Advice must be a deterministic function of authoritative run state and
    code-owned policy knobs. An advisor performs no I/O, holds no authority,
    and its output never feeds back into the active run's decisions.
    """

    @property
    def policy(self) -> ExecutionPolicy: ...

    def advise_route(self, state: RunState, *, signals: RoutingSignals) -> ShadowAdvice: ...

    def advise_context_budget(self, state: RunState) -> ShadowAdvice: ...

    def advise_verification_cadence(self, state: RunState) -> ShadowAdvice: ...
