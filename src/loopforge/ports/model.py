from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.actions import ActionProposal
from loopforge.domain.state import RunState
from loopforge.domain.types import UsageDelta


@dataclass(frozen=True, slots=True)
class ModelTurn:
    action: ActionProposal
    usage: UsageDelta


class ModelContractError(TypeError):
    """Raised when a model adapter violates the runtime response contract."""


class ModelPort(Protocol):
    def propose_action(self, state: RunState) -> ModelTurn: ...
