from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

from loopforge.domain.security import TrustClass
from loopforge.domain.tooling import DataSensitivity
from loopforge.domain.types import ContextItemId, RunId


class ModelRole(StrEnum):
    """The functional role a model turn's context is assembled for."""

    CONTROLLER = "controller"
    PLANNER = "planner"
    REFLECTOR = "reflector"


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptTemplateRef:
    """Reference to the versioned prompt template used for a context assembly."""

    template_id: str
    version: str

    def __post_init__(self) -> None:
        if not self.template_id.strip():
            msg = "prompt template id cannot be empty"
            raise ValueError(msg)
        if not self.version.strip():
            msg_2 = "prompt template version cannot be empty"
            raise ValueError(msg_2)


TRUST_AUTHORITY: Final[dict[TrustClass, int]] = {
    TrustClass.RUNTIME_POLICY: 5,
    TrustClass.AUTHORIZED_HUMAN: 4,
    TrustClass.DETERMINISTIC_OBSERVATION: 3,
    TrustClass.EXTERNAL_EVIDENCE: 2,
    TrustClass.MODEL_INFERENCE: 1,
    TrustClass.UNTRUSTED_CONTENT: 0,
}

# Content from these classes may never be promoted into policy/human authority.
_UNELEVATABLE_TO_AUTHORITY: Final[frozenset[TrustClass]] = frozenset(
    {TrustClass.UNTRUSTED_CONTENT, TrustClass.MODEL_INFERENCE}
)
_AUTHORITY_CLASSES: Final[frozenset[TrustClass]] = frozenset(
    {TrustClass.RUNTIME_POLICY, TrustClass.AUTHORIZED_HUMAN}
)


class ContextAuthorityError(ValueError):
    """Raised when context content attempts an invalid authority elevation."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSource:
    """Provenance record describing where a context item originated."""

    origin: TrustClass
    reference: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.reference.strip():
            msg = "context source reference cannot be empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextItem:
    """A single typed, immutable unit of model context.

    Trust and provenance are intrinsic to the item; they are never inferred
    from the content string itself.
    """

    item_id: ContextItemId
    content: str
    trust: TrustClass
    source: ContextSource
    sensitivity: DataSensitivity = DataSensitivity.INTERNAL
    created_at: datetime
    supersedes: ContextItemId | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.content.strip():
            msg = "context item content cannot be empty"
            raise ValueError(msg)
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            msg_2 = "created_at must be timezone-aware"
            raise ValueError(msg_2)
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                msg_3 = "expires_at must be timezone-aware when set"
                raise ValueError(msg_3)
            if self.expires_at <= self.created_at:
                msg_4 = "expires_at must be after created_at"
                raise ValueError(msg_4)
        if self.supersedes is not None and self.supersedes == self.item_id:
            msg_5 = "context item cannot supersede itself"
            raise ValueError(msg_5)
        if self.trust is not self.source.origin:
            msg_6 = "item trust must match the origin recorded in its source provenance"
            raise ValueError(msg_6)

    def is_fresh(self, now: datetime) -> bool:
        return self.expires_at is None or now < self.expires_at


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextItemSnapshot:
    """Durable, event-friendly snapshot of a ContextItem for replay."""

    item_id: ContextItemId
    content: str
    trust: TrustClass
    source: ContextSource
    sensitivity: DataSensitivity
    created_at: datetime
    supersedes: ContextItemId | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.sensitivity is DataSensitivity.SECRET:
            msg = "secret-sensitivity context must never be persisted to the event store"
            raise ContextAuthorityError(msg)
        # Snapshots cross serialization boundaries, so they re-validate everything
        # a ContextItem would: decoded payloads must not smuggle in invalid state.
        if not self.content.strip():
            msg_2 = "context item content cannot be empty"
            raise ValueError(msg_2)
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            msg_3 = "created_at must be timezone-aware"
            raise ValueError(msg_3)
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                msg_4 = "expires_at must be timezone-aware when set"
                raise ValueError(msg_4)
            if self.expires_at <= self.created_at:
                msg_5 = "expires_at must be after created_at"
                raise ValueError(msg_5)
        if self.supersedes is not None and self.supersedes == self.item_id:
            msg_6 = "context item cannot supersede itself"
            raise ValueError(msg_6)
        if self.trust is not self.source.origin:
            msg_7 = "item trust must match the origin recorded in its source provenance"
            raise ValueError(msg_7)


def snapshot_of(item: ContextItem) -> ContextItemSnapshot:
    return ContextItemSnapshot(
        item_id=item.item_id,
        content=item.content,
        trust=item.trust,
        source=item.source,
        sensitivity=item.sensitivity,
        created_at=item.created_at,
        supersedes=item.supersedes,
        expires_at=item.expires_at,
    )


def promote(item: ContextItem, *, to: TrustClass, basis: str) -> ContextItem:
    """Return a new item at a different trust class, or fail closed.

    Lower-trust content may never silently become higher-authority policy:
    untrusted content and model inference can never be promoted into
    runtime-policy or authorized-human authority, and every promotion
    requires an explicit basis reference (verifier outcome, operator action,
    or equivalent durable evidence). Demotion always succeeds.
    """
    if not basis.strip():
        msg = "trust promotion requires an explicit basis reference"
        raise ContextAuthorityError(msg)
    if (
        TRUST_AUTHORITY[to] > TRUST_AUTHORITY[item.trust]
        and item.trust in _UNELEVATABLE_TO_AUTHORITY
        and to in _AUTHORITY_CLASSES
    ):
        msg_2 = f"{item.trust.value} content can never be promoted to {to.value}"
        raise ContextAuthorityError(msg_2)
    return replace(
        item,
        trust=to,
        source=ContextSource(
            origin=to, reference=basis, detail=f"promoted from {item.trust.value}"
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelContext:
    """The typed, immutable context artifact that crosses the model boundary."""

    run_id: RunId
    items: tuple[ContextItem, ...]
    assembled_at: datetime
    role: ModelRole = ModelRole.CONTROLLER
    prompt_template: PromptTemplateRef | None = None

    def __post_init__(self) -> None:
        if self.assembled_at.tzinfo is None or self.assembled_at.utcoffset() is None:
            msg = "assembled_at must be timezone-aware"
            raise ValueError(msg)
        item_ids = [item.item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            msg_2 = "context item ids must be unique"
            raise ValueError(msg_2)
        known = set(item_ids)
        for item in self.items:
            if item.supersedes is not None and item.supersedes not in known:
                msg_3 = f"superseded item {item.supersedes} is not present in this context"
                raise ValueError(msg_3)
        self._reject_supersession_cycles()

    def _reject_supersession_cycles(self) -> None:
        edges = {
            item.item_id: item.supersedes for item in self.items if item.supersedes is not None
        }
        for start in edges:
            seen: set[ContextItemId] = set()
            current: ContextItemId | None = start
            while current is not None:
                if current in seen:
                    msg_4 = "supersession chain contains a cycle"
                    raise ValueError(msg_4)
                seen.add(current)
                current = edges.get(current)

    def active_items(self, *, now: datetime) -> tuple[ContextItem, ...]:
        superseded = {item.supersedes for item in self.items if item.supersedes is not None}
        return tuple(
            item for item in self.items if item.item_id not in superseded and item.is_fresh(now)
        )

    def by_trust(self, trust: TrustClass) -> tuple[ContextItem, ...]:
        return tuple(item for item in self.items if item.trust is trust)
