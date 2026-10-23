"""Durable working history reconstructed from exact action/result events."""

import json
from collections.abc import Sequence

from loopforge.domain.events import ActionProposed, Event, ToolFailed, ToolSucceeded


def session_history(events: Sequence[Event], *, max_bytes: int = 40000) -> str:
    proposals = {e.proposal.action_id: e.proposal for e in events if isinstance(e, ActionProposed)}
    selected: list[dict[str, object]] = []
    omitted: list[int] = []
    used = 0
    for event in reversed(events):
        if not isinstance(event, (ToolSucceeded, ToolFailed)):
            continue
        proposal = proposals.get(event.action_id)
        if proposal is None:
            continue
        entry: dict[str, object] = {
            "sequence": event.sequence,
            "event_id": str(event.event_id),
            "tool": proposal.tool_name,
            "arguments": dict(proposal.arguments),
            "outcome": "succeeded" if isinstance(event, ToolSucceeded) else "failed",
            "result": event.observation
            if isinstance(event, ToolSucceeded)
            else event.error_message,
        }
        if isinstance(event, ToolFailed):
            entry["error_code"] = event.error_code
        size = len(json.dumps(entry, ensure_ascii=False).encode())
        if used + size > max_bytes:
            omitted.append(event.sequence)
            continue
        used += size
        selected.append(entry)
    return json.dumps(
        {
            "kind": "working_session_history",
            "trust": "untrusted_content",
            "meaning": "Exact historical actions and results, including attempted edits. "
            "Earlier source may have changed; earlier passing checks do not prove current success. "
            "Use this history to avoid repeating failed approaches and reconstruct changes.",
            "actions": list(reversed(selected)),
            "omitted_event_sequences": sorted(omitted),
            "omission_reason": "Whole records omitted when the session byte budget is full",
        },
        ensure_ascii=False,
    )
