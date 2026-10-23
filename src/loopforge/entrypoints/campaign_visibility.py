"""Read-only campaign activity projections for the local automation hub."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.domain.events import (
    ActionProposed,
    ActionRejected,
    ArtifactRecorded,
    BudgetDebited,
    ModelAttemptFailed,
    RunStarted,
    RunStopped,
    ToolFailed,
    ToolSucceeded,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.state import replay
from loopforge.domain.types import RunId
from loopforge.entrypoints.campaign_store import CampaignStore


class ActivityEvent(TypedDict):
    sequence: int
    type: str
    at: str
    message: str


class ActivityArtifact(TypedDict):
    kind: str
    label: str
    content: str


class ActivityRun(TypedDict):
    id: str
    status: str
    started_at: str
    last_activity_at: str
    tokens: int
    iterations: int
    timeline: list[ActivityEvent]
    artifacts: list[ActivityArtifact]
    elapsed_seconds: float
    invalid_model_attempts: int
    unknown_usage_attempts: int
    input_tokens: int
    output_tokens: int
    wall_clock_seconds: float
    total_events: int
    next_event_offset: int | None
    stop_reason: str | None
    provider_metrics: list[dict[str, object]]


class ActivityProjection(TypedDict):
    runs: list[ActivityRun]
    limits: dict[str, object]
    pagination: dict[str, int | None]


_PRE_REQUEST_STOPS = frozenset(
    {
        "CODING_REQUIRED_CHECK_CIRCUIT_OPEN",
        "CODING_AUTOMATIC_CHECK_BLOCKED",
        "CODING_NO_NEW_EVIDENCE",
        "REVIEW_REPORT_CIRCUIT_OPEN",
        "REVIEW_SOURCE_CIRCUIT_OPEN",
        "REVIEW_NO_NEW_EVIDENCE",
        "MODEL_DEADLINE",
        "MODEL_CONTEXT_LIMIT",
        "REPORT_PACKET_LIMIT",
        "REVIEW_REQUIRED_READ_LIMIT",
        "REVIEW_REQUIRED_EVIDENCE_INVALID",
    }
)


def unknown_provider_usage(reason_code: str, debited: bool) -> bool:
    """A local pre-request stop consumes no provider turn to account for."""
    return reason_code == "REPORT_USAGE_UNKNOWN" or (
        not debited and reason_code not in _PRE_REQUEST_STOPS
    )


def campaign_activity(  # noqa: PLR0912, PLR0913, PLR0915 - bounded evidence-only projection
    directory: Path,
    store: CampaignStore,
    campaign: str,
    *,
    run_offset: int = 0,
    run_limit: int = 20,
    event_offset: int = 0,
    event_limit: int = 100,
    run_id: str | None = None,
) -> ActivityProjection:
    if not (
        run_offset >= 0 and 1 <= run_limit <= 100 and event_offset >= 0 and 1 <= event_limit <= 500
    ):
        msg = "Invalid activity pagination"
        raise ValueError(msg)
    with store.connect() as db:
        config = store.config(db, campaign)
        references: list[str] = []
        metrics: dict[str, list[dict[str, object]]] = {}
        for row in db.execute(
            "SELECT detail FROM campaign_events WHERE campaign=? ORDER BY sequence", (campaign,)
        ):
            detail = json.loads(row["detail"])
            reference = detail.get("run_id")
            if isinstance(reference, str):
                if reference not in references:
                    references.append(reference)
                observed = detail.get("provider_observations")
                if isinstance(observed, list):
                    metrics[reference] = [
                        cast("dict[str, object]", item)
                        for item in cast("list[object]", observed)
                        if isinstance(item, dict)
                    ][-256:]
    total = len(references)
    if run_id is not None:
        if run_id not in references:
            msg = "Run does not belong to this campaign"
            raise ValueError(msg)
        references = [run_id]
    else:
        references = list(reversed(list(reversed(references))[run_offset : run_offset + run_limit]))
    pagination = {
        "total_runs": total,
        "run_offset": run_offset,
        "next_run_offset": run_offset + run_limit if run_offset + run_limit < total else None,
        "event_offset": event_offset,
        "event_limit": event_limit,
    }
    if not (directory / "runs.sqlite").exists():
        return {"runs": [], "limits": config.model_dump(), "pagination": pagination}
    events_store = SQLiteEventStore(directory / "runs.sqlite", codec=JsonEventCodec())
    runs: list[ActivityRun] = []
    for reference in references:
        events = events_store.events_for(RunId(reference))
        if not events:
            continue
        state = replay(RunId(reference), events)
        timeline: list[ActivityEvent] = []
        artifacts: list[ActivityArtifact] = []
        tokens = 0
        turns = 0
        for event in events:
            message: str | None = None
            if isinstance(event, ActionProposed):
                turns += 1
                message = f"Proposed {event.proposal.tool_name}"
            elif isinstance(event, ActionRejected):
                message = f"Rejected {event.proposal.tool_name}: {event.reason_code}"
            elif isinstance(event, ToolSucceeded):
                message = event.observation[:2000]
            elif isinstance(event, ToolFailed):
                message = f"{event.error_code}: {event.error_message[:2000]}"
            elif isinstance(event, (VerificationPassed, VerificationFailed)):
                message = event.summary[:2000]
            elif isinstance(event, RunStarted):
                message = event.objective
            elif isinstance(event, RunStopped):
                message = event.summary
            elif isinstance(event, ModelAttemptFailed):
                message = f"Model attempt failed: {event.reason_code}: {event.summary}"
            elif isinstance(event, BudgetDebited):
                tokens += event.usage.input_tokens + event.usage.output_tokens
            elif isinstance(event, ArtifactRecorded):
                artifacts.append(
                    {"kind": event.kind.value, "label": event.label, "content": event.content}
                )
            if message is not None:
                timeline.append(
                    {
                        "sequence": event.sequence,
                        "type": type(event).__name__,
                        "at": event.occurred_at.isoformat(),
                        "message": message,
                    }
                )
        runs.append(
            {
                "id": reference,
                "status": state.status.value,
                "provider_metrics": metrics.get(reference, []),
                "started_at": events[0].occurred_at.isoformat(),
                "last_activity_at": events[-1].occurred_at.isoformat(),
                "tokens": tokens,
                "iterations": turns,
                "timeline": list(
                    reversed(list(reversed(timeline))[event_offset : event_offset + event_limit])
                ),
                "artifacts": artifacts,
                "total_events": len(timeline),
                "next_event_offset": (
                    event_offset + event_limit
                    if event_offset + event_limit < len(timeline)
                    else None
                ),
                "wall_clock_seconds": max(
                    0,
                    (
                        (events[-1].occurred_at if state.status.is_terminal else datetime.now(UTC))
                        - events[0].occurred_at
                    ).total_seconds(),
                ),
                "elapsed_seconds": max(
                    0, (events[-1].occurred_at - events[0].occurred_at).total_seconds()
                ),
                "invalid_model_attempts": sum(isinstance(e, ModelAttemptFailed) for e in events),
                "unknown_usage_attempts": sum(
                    isinstance(e, ModelAttemptFailed)
                    and unknown_provider_usage(
                        e.reason_code,
                        index + 1 < len(events) and isinstance(events[index + 1], BudgetDebited),
                    )
                    for index, e in enumerate(events)
                ),
                "input_tokens": state.input_tokens,
                "output_tokens": state.output_tokens,
                "stop_reason": state.stop_reason.value if state.stop_reason else None,
            }
        )
    return {"runs": runs, "limits": config.model_dump(), "pagination": pagination}
