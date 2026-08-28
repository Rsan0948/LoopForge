from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext
from loopforge.domain.types import UsageDelta


@dataclass(frozen=True, slots=True)
class ModelTurn:
    action: ActionProposal
    usage: UsageDelta


class ModelContractError(TypeError):
    """Raised when a model adapter violates the runtime response contract."""


class ModelFailureClass(StrEnum):
    """Operational classification of a model-turn failure.

    This is runtime vocabulary, not provider vocabulary: adapters normalize
    provider-specific errors (HTTP statuses, transport failures, malformed
    payloads) into these classes so the runtime can make stopping and retry
    decisions without any provider semantics crossing the boundary.
    """

    TRANSIENT = "transient"
    PERMANENT = "permanent"


class ModelTurnError(RuntimeError):
    """A normalized, classified model-turn failure raised by live adapters.

    ``reason_code`` is a machine-stable, provider-independent code (for
    example ``MODEL_UNAVAILABLE`` or ``MODEL_INVALID_RESPONSE``) that the
    runtime records as the stop reason when the failure is terminal.
    """

    def __init__(self, failure_class: ModelFailureClass, reason_code: str, summary: str) -> None:
        super().__init__(summary)
        # Coerce through the enum so plain-string classes ("permanent") cannot
        # bypass identity checks at the runtime boundary.
        self.failure_class = ModelFailureClass(failure_class)
        if not reason_code.strip():
            msg = "model turn error reason_code cannot be empty"
            raise ValueError(msg)
        self.reason_code = reason_code
        self.summary = summary


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelToolSpec:
    """Code-owned description of one tool a live model may propose.

    This catalog *describes* the runtime's registered tools for the provider;
    it never defines or downgrades tool risk, permission, side-effect, retry,
    idempotency, approval, timeout, or sensitivity metadata — those remain
    code-owned on ``ToolMetadata`` and are re-authorized on every proposal
    (AGENTS.md rule 4). ``parameters`` is a provider-independent JSON Schema
    object describing the tool's string-valued arguments.
    """

    name: str
    description: str
    parameters: dict[str, object]

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "model tool spec name cannot be empty"
            raise ValueError(msg)
        if not self.description.strip():
            msg_2 = "model tool spec description cannot be empty"
            raise ValueError(msg_2)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCapabilities:
    """Provider/model capability metadata advertised by a live adapter.

    Honest, code-owned metadata about the backing model. Cost rates are per
    one million tokens in USD; local providers report ``0.0``. The capability
    registry and routing policy are a follow-on cycle (PACS-012).
    """

    provider: str
    model: str
    supports_tool_calls: bool
    context_window_tokens: int
    input_cost_usd_per_million: float = 0.0
    output_cost_usd_per_million: float = 0.0

    def __post_init__(self) -> None:
        if not self.provider.strip():
            msg = "model capabilities provider cannot be empty"
            raise ValueError(msg)
        if not self.model.strip():
            msg_2 = "model capabilities model cannot be empty"
            raise ValueError(msg_2)
        if not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.context_window_tokens, int
        ) or isinstance(self.context_window_tokens, bool):
            msg_5 = "model capabilities context window must be an integer"
            raise ValueError(msg_5)  # noqa: TRY004
        if self.context_window_tokens <= 0:
            msg_3 = "model capabilities context window must be positive"
            raise ValueError(msg_3)
        rates = (self.input_cost_usd_per_million, self.output_cost_usd_per_million)
        if not all(math.isfinite(rate) for rate in rates):
            msg_6 = "model capabilities cost rates must be finite"
            raise ValueError(msg_6)
        if self.input_cost_usd_per_million < 0 or self.output_cost_usd_per_million < 0:
            msg_4 = "model capabilities cost rates cannot be negative"
            raise ValueError(msg_4)


class ModelPort(Protocol):
    def propose_action(self, context: ModelContext) -> ModelTurn: ...
