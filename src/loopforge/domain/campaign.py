"""Campaign checkpoints are projections of events, independent of run state."""

from dataclasses import dataclass, replace
from typing import Literal

LANES = ("map", "bugs", "boundaries", "contracts", "docs", "challenge", "reconcile")
CampaignStatus = Literal[
    "ready", "running", "paused", "attention", "checkpoint", "parked", "complete"
]
EventKind = Literal[
    "claimed",
    "coding_claimed",
    "linked",
    "finished",
    "failed",
    "pause",
    "cancel",
    "resume",
    "advance",
    "complete",
    "model_changed",
    "park",
    "assessed",
    "review_defined",
]


@dataclass(frozen=True)
class CampaignState:
    stage: int = 1
    cursor: int = 0
    status: CampaignStatus = "ready"
    task_id: str | None = None
    pause_requested: bool = False
    cancel_requested: bool = False
    return_status: CampaignStatus | None = None
    parked_status: CampaignStatus | None = None


@dataclass(frozen=True)
class CampaignEvent:
    kind: EventKind
    task_id: str | None = None
    model: str | None = None


def reduce_campaign(  # noqa: PLR0911, PLR0912 - explicit legal event arms
    state: CampaignState,
    event: CampaignEvent,
    *,
    task_count: int,
) -> CampaignState:
    """Fail closed on illegal transitions; stage changes require an operator command."""
    if state.status == "complete":
        msg = "a completed campaign cannot become active again"
        raise ValueError(msg)
    if event.kind in {"assessed", "review_defined"}:
        # Finding dispositions are evidence only, never runtime authority.
        return state
    if event.kind == "model_changed" and state.status in {
        "paused",
        "attention",
        "checkpoint",
        "parked",
    }:
        if not event.model:
            msg = "model identity is required"
            raise ValueError(msg)
        return state
    if event.kind == "park" and state.status in {"ready", "paused", "attention", "checkpoint"}:
        return replace(state, status="parked", parked_status=state.status)
    if event.kind == "claimed" and state.status == "ready" and event.task_id:
        return replace(state, status="running", task_id=event.task_id, cancel_requested=False)
    if event.kind == "coding_claimed" and state.status in {"ready", "checkpoint", "paused"}:
        if not event.task_id:
            msg = "coding task identity is required"
            raise ValueError(msg)
        return replace(
            state,
            status="running",
            task_id=event.task_id,
            return_status=state.status,
            cancel_requested=False,
        )
    if event.kind == "linked" and state.status == "running" and event.task_id == state.task_id:
        return state
    if event.kind in {"finished", "failed"} and state.status == "running":
        if event.task_id != state.task_id:
            msg = "task identity does not match the active task"
            raise ValueError(msg)
        if event.kind == "failed":
            return replace(state, status="attention", task_id=None, cancel_requested=False)
        if state.return_status is not None:
            return replace(
                state,
                status="paused" if state.pause_requested else state.return_status,
                task_id=None,
                return_status=None,
                cancel_requested=False,
            )
        cursor = state.cursor + 1
        return replace(
            state,
            cursor=cursor,
            status="checkpoint"
            if cursor >= task_count
            else "paused"
            if state.pause_requested
            else "ready",
            task_id=None,
        )
    if event.kind == "cancel" and state.status == "running":
        return replace(state, cancel_requested=True)
    if event.kind == "pause" and state.status == "ready":
        return replace(state, status="paused")
    if event.kind == "pause" and state.status == "running":
        return replace(state, pause_requested=True)
    if event.kind == "resume" and state.status in {"paused", "attention", "parked"}:
        return replace(
            state,
            status="checkpoint"
            if state.parked_status == "checkpoint"
            else state.return_status or "ready",
            pause_requested=False,
            return_status=None,
            parked_status=None,
        )
    if event.kind == "advance" and state.status == "checkpoint" and state.stage == 1:
        return CampaignState(stage=2)
    if event.kind == "complete" and state.status in {"checkpoint", "paused", "attention", "parked"}:
        return replace(state, status="complete")
    msg = f"illegal campaign transition: {state.status} -> {event.kind}"
    raise ValueError(msg)
