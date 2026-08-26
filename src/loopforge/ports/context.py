from __future__ import annotations

from typing import Protocol

from loopforge.domain.context import ModelContext
from loopforge.domain.state import RunState


class ContextContractError(TypeError):
    """Raised when a context builder adapter violates the context contract."""


class ContextBuilderPort(Protocol):
    """Assembles the typed ModelContext artifact for a model turn.

    Role-specific assembly, token budgeting, and compaction are later-cycle
    concerns; this port only guarantees a typed, immutable boundary artifact.
    """

    def build_context(self, state: RunState) -> ModelContext: ...
