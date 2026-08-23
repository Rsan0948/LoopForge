from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from loopforge.domain.types import ActionId


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """A model-requested action.

    Security and reliability semantics are intentionally absent: risk, permission,
    retry, idempotency, approval, and timeout policy belong to the registered tool
    contract, not to model output.
    """

    action_id: ActionId
    tool_name: str
    arguments: Mapping[str, str]
    expected_observation: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            msg = "tool_name cannot be empty"
            raise ValueError(msg)
