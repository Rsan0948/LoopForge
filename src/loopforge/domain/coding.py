"""Deterministic lifecycle for a serial coding task and its independent review."""

from dataclasses import dataclass, replace
from typing import Literal

CodingStatus = Literal[
    "queued",
    "coding",
    "review_pending",
    "reviewing",
    "patch_ready",
    "attention",
    "accepted",
    "rejected",
]
CodingKind = Literal[
    "start",
    "linked",
    "patched",
    "review",
    "reviewed",
    "failed",
    "retry",
    "retry_review",
    "request_changes",
    "accept",
    "reject",
]


@dataclass(frozen=True)
class CodingState:
    status: CodingStatus = "queued"
    attempts: int = 0
    run_id: str | None = None
    review_attempts: int = 0
    failure_stage: str | None = None


@dataclass(frozen=True)
class CodingEvent:
    kind: CodingKind
    run_id: str | None = None


def reduce_coding(  # noqa: PLR0911 - explicit legal transitions
    state: CodingState, event: CodingEvent, *, max_attempts: int, max_review_attempts: int = 3
) -> CodingState:
    if event.kind == "start" and state.status == "queued" and state.attempts < max_attempts:
        return replace(
            state, status="coding", attempts=state.attempts + 1, run_id=None, failure_stage=None
        )
    if event.kind == "linked" and state.status in {"coding", "reviewing"} and event.run_id:
        return replace(state, run_id=event.run_id)
    if event.kind == "patched" and state.status == "coding":
        return replace(state, status="review_pending")
    if (
        event.kind == "review"
        and state.status == "review_pending"
        and state.review_attempts < max_review_attempts
    ):
        return replace(
            state, status="reviewing", run_id=None, review_attempts=state.review_attempts + 1
        )
    if event.kind == "reviewed" and state.status == "reviewing":
        return replace(state, status="patch_ready")
    if event.kind == "failed" and state.status in {"coding", "reviewing"}:
        return replace(state, status="attention", failure_stage=state.status)
    if (
        event.kind == "retry_review"
        and state.status == "attention"
        and state.failure_stage == "reviewing"
        and state.review_attempts < max_review_attempts
    ):
        return replace(state, status="review_pending", run_id=None)
    if event.kind == "retry" and state.status == "attention" and state.attempts < max_attempts:
        return replace(state, status="queued", run_id=None)
    if event.kind == "accept" and state.status == "patch_ready":
        return replace(state, status="accepted")
    if (
        event.kind == "request_changes"
        and (
            state.status == "patch_ready"
            or (state.status == "attention" and state.failure_stage == "reviewing")
        )
        and state.attempts < max_attempts
        and state.review_attempts < max_review_attempts
    ):
        return replace(state, status="queued", run_id=None, failure_stage=None)
    if event.kind == "reject" and state.status in {"patch_ready", "attention", "queued"}:
        return replace(state, status="rejected")
    msg = f"illegal coding transition: {state.status} -> {event.kind}"
    raise ValueError(msg)
