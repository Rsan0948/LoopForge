from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

from loopforge.domain.context import TRUST_AUTHORITY, ContextItem, ModelRole
from loopforge.domain.types import ContextItemId


class PreservationClass(StrEnum):
    """Context content classes that must survive selection and compaction."""

    OBJECTIVE = "objective"
    BLOCKER = "blocker"
    VERIFIER_FAILURE = "verifier_failure"
    CONFIRMED_FACT = "confirmed_fact"
    PENDING_APPROVAL = "pending_approval"
    IRREVERSIBLE_ACTION = "irreversible_action"


class DropReason(StrEnum):
    """Why a candidate context item was excluded from the final assembly."""

    EXCLUDED_BY_ROLE = "excluded_by_role"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    OVER_BUDGET = "over_budget"


class ContextBudgetError(ValueError):
    """Raised when required context cannot fit the requested token budget."""


TRUNCATION_MARKER: Final[str] = "\n…[truncated]"

ALL_ROLES: Final[frozenset[ModelRole]] = frozenset(ModelRole)
NO_PRESERVATION: Final[frozenset[PreservationClass]] = frozenset()


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextTokenBudget:
    """Hard token budget for one context assembly.

    `reserve_tokens` is headroom deliberately left unused (for example, for a
    model's response), so content may consume at most
    `max_tokens - reserve_tokens`.
    """

    max_tokens: int
    reserve_tokens: int = 0

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            msg = "max_tokens must be positive"
            raise ValueError(msg)
        if self.reserve_tokens < 0:
            msg_2 = "reserve_tokens cannot be negative"
            raise ValueError(msg_2)
        if self.reserve_tokens >= self.max_tokens:
            msg_3 = "reserve_tokens must leave room for context content"
            raise ValueError(msg_3)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCandidate:
    """A context item plus the selection metadata budgeting operates on.

    Preservation classes are assigned by the code that created the candidate,
    never inferred from content. `compactible` marks whether truncation is an
    acceptable alternative to dropping under budget pressure; preserved
    candidates are always kept whole or the build fails explicitly.
    """

    item: ContextItem
    preserved: frozenset[PreservationClass] = NO_PRESERVATION
    compactible: bool = True
    roles: frozenset[ModelRole] = ALL_ROLES


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountingEntry:
    """Per-candidate ledger record for explicit context-budget accounting."""

    item_id: ContextItemId
    tokens: int
    kept: bool
    compacted: bool = False
    preserved: frozenset[PreservationClass] = NO_PRESERVATION
    drop_reason: DropReason | None = None

    def __post_init__(self) -> None:
        if self.tokens < 0:
            msg = "accounting tokens cannot be negative"
            raise ValueError(msg)
        if self.kept and self.drop_reason is not None:
            msg_2 = "kept entries cannot record a drop reason"
            raise ValueError(msg_2)
        if not self.kept and self.drop_reason is None:
            msg_3 = "dropped entries must record a drop reason"
            raise ValueError(msg_3)
        if not self.kept and self.tokens != 0:
            msg_4 = "dropped entries cannot consume budget"
            raise ValueError(msg_4)
        if self.compacted and not self.kept:
            msg_5 = "compacted entries must be kept"
            raise ValueError(msg_5)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextAccounting:
    """Explicit token ledger for one context assembly.

    Construction rejects any ledger whose total exceeds the budget's usable
    limit, so an over-budget assembly can never be represented.
    """

    budget: ContextTokenBudget
    entries: tuple[AccountingEntry, ...]
    overhead_tokens: int = 0

    def __post_init__(self) -> None:
        if self.overhead_tokens < 0:
            msg = "overhead_tokens cannot be negative"
            raise ValueError(msg)
        if self.used_tokens > self.usable_tokens:
            msg_2 = (
                f"accounting uses {self.used_tokens} tokens but the budget allows "
                f"{self.usable_tokens}"
            )
            raise ContextBudgetError(msg_2)

    @property
    def usable_tokens(self) -> int:
        return self.budget.max_tokens - self.budget.reserve_tokens

    @property
    def content_tokens(self) -> int:
        return sum(entry.tokens for entry in self.entries if entry.kept)

    @property
    def used_tokens(self) -> int:
        return self.overhead_tokens + self.content_tokens

    @property
    def kept_entries(self) -> tuple[AccountingEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kept)

    @property
    def dropped_entries(self) -> tuple[AccountingEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.kept)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSelection:
    """The result of deterministic selection: final items plus their ledger."""

    role: ModelRole
    items: tuple[ContextItem, ...]
    accounting: ContextAccounting

    def __post_init__(self) -> None:
        item_ids = {item.item_id for item in self.items}
        kept_ids = {entry.item_id for entry in self.accounting.kept_entries}
        if item_ids != kept_ids:
            msg = "selected items must match the kept accounting entries"
            raise ValueError(msg)


def truncate_content(
    content: str,
    *,
    allowance_tokens: int,
    count_tokens: Callable[[str], int],
) -> str | None:
    """Deterministically truncate content to fit a token allowance.

    Returns the original content when it already fits, the longest prefix
    carrying at least one original character plus the truncation marker that
    fits the allowance, or None when no such prefix exists.
    """
    if count_tokens(content) <= allowance_tokens:
        return content
    if allowance_tokens <= 0:
        return None
    if count_tokens(TRUNCATION_MARKER) > allowance_tokens:
        return None
    low, high = 1, len(content)
    best: str | None = None
    while low <= high:
        mid = (low + high) // 2
        candidate = content[:mid] + TRUNCATION_MARKER
        if count_tokens(candidate) <= allowance_tokens:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


_SelectionResult = tuple[ContextItem | None, int, bool, DropReason | None]


def _measured_tokens(content: str, count_tokens: Callable[[str], int]) -> int:
    """Measure content, rejecting misbehaving counters before they corrupt the ledger."""
    tokens = count_tokens(content)
    if tokens < 0:
        msg = "token counts cannot be negative"
        raise ValueError(msg)
    return tokens


def _role_scope(
    candidates: tuple[ContextCandidate, ...], role: ModelRole
) -> tuple[list[ContextCandidate], dict[ContextItemId, AccountingEntry]]:
    eligible: list[ContextCandidate] = []
    dropped: dict[ContextItemId, AccountingEntry] = {}
    for candidate in candidates:
        if role in candidate.roles:
            eligible.append(candidate)
            continue
        dropped[candidate.item.item_id] = AccountingEntry(
            item_id=candidate.item.item_id,
            tokens=0,
            kept=False,
            preserved=candidate.preserved,
            drop_reason=DropReason.EXCLUDED_BY_ROLE,
        )
    return eligible, dropped


def _live_candidates(
    eligible: list[ContextCandidate], now: datetime
) -> tuple[list[ContextCandidate], dict[ContextItemId, AccountingEntry]]:
    superseded = {
        candidate.item.supersedes for candidate in eligible if candidate.item.supersedes is not None
    }
    live: list[ContextCandidate] = []
    dead: dict[ContextItemId, AccountingEntry] = {}
    for candidate in eligible:
        if candidate.item.item_id in superseded:
            reason = DropReason.SUPERSEDED
        elif not candidate.item.is_fresh(now):
            reason = DropReason.EXPIRED
        else:
            live.append(candidate)
            continue
        dead[candidate.item.item_id] = AccountingEntry(
            item_id=candidate.item.item_id,
            tokens=0,
            kept=False,
            preserved=candidate.preserved,
            drop_reason=reason,
        )
    return live, dead


def _allocate_preserved(
    live: list[ContextCandidate],
    *,
    limit: int,
    count_tokens: Callable[[str], int],
    results: dict[ContextItemId, _SelectionResult],
    kept_order: list[ContextItem],
) -> int:
    """Allocate preserved candidates whole, in input order, or fail explicitly."""
    used = 0
    for candidate in live:
        if not candidate.preserved:
            continue
        tokens = _measured_tokens(candidate.item.content, count_tokens)
        used += tokens
        results[candidate.item.item_id] = (candidate.item, tokens, False, None)
        kept_order.append(candidate.item)
    if used > limit:
        msg = f"preserved context requires {used} tokens but the budget allows {limit}"
        raise ContextBudgetError(msg)
    return used


def _fill_remainder(  # noqa: PLR0913 - shared selection state is threaded explicitly
    live: list[ContextCandidate],
    *,
    limit: int,
    used: int,
    count_tokens: Callable[[str], int],
    results: dict[ContextItemId, _SelectionResult],
    kept_order: list[ContextItem],
) -> None:
    """Greedily fill the remaining budget by trust/recency rank, truncating last."""

    def rank(candidate: ContextCandidate) -> tuple[int, datetime, str]:
        item = candidate.item
        return (TRUST_AUTHORITY[item.trust], item.created_at, item.item_id)

    remainder = sorted(
        (candidate for candidate in live if not candidate.preserved),
        key=rank,
        reverse=True,
    )
    for candidate in remainder:
        item = candidate.item
        tokens = _measured_tokens(item.content, count_tokens)
        if used + tokens <= limit:
            used += tokens
            results[item.item_id] = (item, tokens, False, None)
            kept_order.append(item)
            continue
        if candidate.compactible:
            truncated = truncate_content(
                item.content,
                allowance_tokens=limit - used,
                count_tokens=count_tokens,
            )
            # The item does not fit whole, so any truncation result here is a
            # strictly shorter, marker-terminated variant of the original.
            if truncated is not None:
                compacted_item = replace(item, content=truncated)
                compacted_tokens = _measured_tokens(truncated, count_tokens)
                used += compacted_tokens
                results[item.item_id] = (compacted_item, compacted_tokens, True, None)
                kept_order.append(compacted_item)
                continue
        results[item.item_id] = (None, 0, False, DropReason.OVER_BUDGET)


def _prune_dangling_edges(kept_order: list[ContextItem]) -> tuple[ContextItem, ...]:
    """Prune supersession edges to dropped items.

    This bookkeeping guarantees the final items always satisfy the
    ModelContext construction contract; item content, trust, and provenance
    are untouched.
    """
    kept_ids = {item.item_id for item in kept_order}
    return tuple(
        replace(item, supersedes=None)
        if item.supersedes is not None and item.supersedes not in kept_ids
        else item
        for item in kept_order
    )


def _ledger(
    candidates: tuple[ContextCandidate, ...],
    *,
    role_dropped: dict[ContextItemId, AccountingEntry],
    dead: dict[ContextItemId, AccountingEntry],
    results: dict[ContextItemId, _SelectionResult],
) -> tuple[AccountingEntry, ...]:
    entries: list[AccountingEntry] = []
    for candidate in candidates:
        item_id = candidate.item.item_id
        if item_id in role_dropped:
            entries.append(role_dropped[item_id])
            continue
        if item_id in dead:
            entries.append(dead[item_id])
            continue
        final_item, tokens, compacted, reason = results[item_id]
        entries.append(
            AccountingEntry(
                item_id=item_id,
                tokens=tokens,
                kept=final_item is not None,
                compacted=compacted,
                preserved=candidate.preserved,
                drop_reason=reason,
            )
        )
    return tuple(entries)


def select_context(  # noqa: PLR0913 - budget, role, clock, and counter are explicit policy inputs
    candidates: tuple[ContextCandidate, ...],
    *,
    budget: ContextTokenBudget,
    role: ModelRole,
    count_tokens: Callable[[str], int],
    now: datetime,
    overhead_tokens: int = 0,
) -> ContextSelection:
    """Select, budget, and compact candidate context deterministically.

    Processing order is fixed: role scoping, then supersession/freshness, then
    preserved allocation, then ranked greedy fill with truncation. Guarantees:

    - every live, role-eligible preserved candidate is kept whole, or the
      selection fails explicitly with ContextBudgetError;
    - compaction only ever drops or truncates content; trust and provenance
      are never altered, so nothing is silently elevated or demoted;
    - equivalent candidates, policy, and budget produce an equivalent
      selection.
    """
    if overhead_tokens < 0:
        msg = "overhead_tokens cannot be negative"
        raise ValueError(msg)
    candidate_ids = [candidate.item.item_id for candidate in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        msg_2 = "context candidate item ids must be unique"
        raise ValueError(msg_2)
    limit = budget.max_tokens - budget.reserve_tokens - overhead_tokens
    if limit <= 0:
        msg_3 = "overhead and reserve consume the entire token budget"
        raise ContextBudgetError(msg_3)

    eligible, role_dropped = _role_scope(candidates, role)
    live, dead = _live_candidates(eligible, now)

    results: dict[ContextItemId, _SelectionResult] = {}
    kept_order: list[ContextItem] = []
    used = _allocate_preserved(
        live, limit=limit, count_tokens=count_tokens, results=results, kept_order=kept_order
    )
    _fill_remainder(
        live,
        limit=limit,
        used=used,
        count_tokens=count_tokens,
        results=results,
        kept_order=kept_order,
    )

    return ContextSelection(
        role=role,
        items=_prune_dangling_edges(kept_order),
        accounting=ContextAccounting(
            budget=budget,
            entries=_ledger(candidates, role_dropped=role_dropped, dead=dead, results=results),
            overhead_tokens=overhead_tokens,
        ),
    )
