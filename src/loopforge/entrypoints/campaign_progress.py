"""Durable fresh-state progress projection shared by escalation and stopping."""

import hashlib
import json
from collections.abc import Sequence

from loopforge.domain.events import ActionProposed, ContextAssembled, Event, ToolSucceeded
from loopforge.domain.security import TrustClass


def current_patch(event: ContextAssembled) -> str | None:
    packet = next(
        (
            i
            for i in event.context_items
            if str(i.item_id).endswith(":continuation-state")
            and i.source.reference == "runtime:current-workspace-and-event-projection"
        ),
        None,
    )
    if packet is None:
        return None
    value = json.loads(packet.content).get("current_patch_sha256")
    return value if isinstance(value, str) else None


def stagnant_turns(events: Sequence[Event]) -> int:
    proposals = {e.proposal.action_id: e.proposal for e in events if isinstance(e, ActionProposed)}
    reads: set[str] = set()
    previous: tuple[str, frozenset[str], tuple[str, ...]] | None = None
    stale = 0
    for event in events:
        if isinstance(event, ToolSucceeded):
            proposal = proposals.get(event.action_id)
            if proposal is not None and proposal.tool_name == "read_file":
                reads.add(hashlib.sha256(event.observation.encode()).hexdigest())
        if not isinstance(event, ContextAssembled):
            continue
        patch = current_patch(event)
        if patch is None:
            continue
        # Only a new, journal-projected specialist diagnosis is new evidence.
        # Repeating the acknowledgement action or mentioning advice in source is not.
        advice = tuple(
            hashlib.sha256(i.content.encode()).hexdigest()
            for i in event.context_items
            if str(i.item_id).endswith(":repair-specialist")
            and i.trust is TrustClass.MODEL_INFERENCE
            and i.source.origin is TrustClass.MODEL_INFERENCE
            and i.source.reference == "repair-ledger:latest-settled"
        )
        signature = (patch, frozenset(reads), advice)
        stale = stale + 1 if signature == previous else 0
        previous = signature
    return stale
