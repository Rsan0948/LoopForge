from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext
from loopforge.domain.routing import ModelCapabilities
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


# ModelCapabilities lives in ``loopforge.domain.routing`` (code-owned domain
# vocabulary, mirroring ``SandboxCapabilities``); it is imported here so the
# port can declare it structurally.


class ModelPort(Protocol):
    @property
    def capabilities(self) -> ModelCapabilities:
        """Honest capability metadata for the backing model.

        Every adapter advertises capabilities — deterministic and scripted
        adapters included — so the registry and routing policy can match
        code-owned ``ModelRequirements`` fail-closed instead of trusting
        provider names (mirrors ``SandboxPort.capabilities``).
        """
        ...

    def propose_action(self, context: ModelContext) -> ModelTurn: ...
