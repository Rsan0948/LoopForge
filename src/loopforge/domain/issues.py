"""Serial investigation lifecycle; model reports never close operator-owned issues."""

from dataclasses import dataclass, replace
from typing import Literal

IssueStatus = Literal["queued", "running", "investigated", "attention", "rejected", "closed"]


@dataclass(frozen=True, slots=True)
class IssueState:
    status: IssueStatus = "queued"
    attempts: int = 0
    tokens: int = 0
    seconds: float = 0
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class IssueEvent:
    kind: Literal["start", "linked", "investigated", "failed", "retry", "reject", "close"]
    run_id: str | None = None
    tokens: int = 0
    seconds: float = 0


def reduce_issue(
    state: IssueState,
    event: IssueEvent,
    *,
    max_attempts: int,
    max_tokens: int,
    max_seconds: float = 1800,
) -> IssueState:
    if event.tokens < 0 or event.seconds < 0:
        msg = "issue consumption cannot be negative"
        raise ValueError(msg)
    if state.status in {"closed", "rejected"}:
        msg = "terminal issue cannot become active"
        raise ValueError(msg)
    if event.kind == "start" and state.status == "queued":
        if (
            state.attempts >= max_attempts
            or state.tokens >= max_tokens
            or state.seconds >= max_seconds
        ):
            msg = "issue allowance exhausted"
            raise ValueError(msg)
        return replace(state, status="running", attempts=state.attempts + 1, run_id=None)
    if event.kind == "linked" and state.status == "running" and event.run_id:
        return replace(state, run_id=event.run_id)
    if event.kind in {"investigated", "failed"} and state.status == "running":
        return replace(
            state,
            status="investigated" if event.kind == "investigated" else "attention",
            tokens=state.tokens + event.tokens,
            seconds=state.seconds + event.seconds,
        )
    if event.kind == "retry" and state.status == "attention":
        if (
            state.attempts >= max_attempts
            or state.tokens >= max_tokens
            or state.seconds >= max_seconds
        ):
            msg = "issue allowance exhausted"
            raise ValueError(msg)
        return replace(state, status="queued", run_id=None)
    if event.kind == "reject" and state.status in {"queued", "attention", "investigated"}:
        return replace(state, status="rejected")
    if event.kind == "close" and state.status == "investigated":
        return replace(state, status="closed")
    msg = f"illegal issue transition: {state.status} -> {event.kind}"
    raise ValueError(msg)
