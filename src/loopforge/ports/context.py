from __future__ import annotations

from typing import Protocol

from loopforge.domain.context import ModelContext, ModelRole
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.state import RunState


class ContextContractError(TypeError):
    """Raised when a context builder adapter violates the context contract."""


class TokenCounterPort(Protocol):
    """Deterministically estimates the token cost of context text.

    The counter is a budgeting heuristic owned by the runtime, not a provider
    tokenizer; provider-specific counting stays quarantined in adapters.
    """

    def count_tokens(self, text: str) -> int: ...


class ContextBuilderPort(Protocol):
    """Assembles the typed ModelContext artifact for a model turn.

    Builders must return a typed, immutable boundary artifact. When a token
    budget is supplied (or configured on the adapter), the assembled context
    must deterministically fit that budget or fail explicitly.
    """

    def build_context(
        self,
        state: RunState,
        *,
        role: ModelRole = ModelRole.CONTROLLER,
        token_budget: ContextTokenBudget | None = None,
    ) -> ModelContext: ...
