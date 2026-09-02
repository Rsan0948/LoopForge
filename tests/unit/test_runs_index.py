"""Runs-index projection: status mapping, fold, and the in-memory list_runs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.sqlite_events import (
    _RunIndexRow,  # pyright: ignore[reportPrivateUsage]  # index row pinned directly
    fold_run_index_row,
)
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import (
    ActionProposed,
    ApprovalGranted,
    ApprovalRequested,
    BudgetDebited,
    Event,
    ModelTurnRecorded,
    OperatorInstruction,
    PlanCreated,
    RunStarted,
    RunStopped,
    WorkerMerged,
)
from loopforge.domain.orchestration import MergeOutcome
from loopforge.domain.state import RunRecord, status_after_event, summarize_run
from loopforge.domain.types import (
    ActionId,
    EventId,
    RunId,
    RunStatus,
    StopReason,
    UsageDelta,
    WorkerId,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
RUN = RunId("index-run")


def _started(sequence: int = 1, *, run_id: RunId = RUN, objective: str = "repair") -> RunStarted:
    return RunStarted(
        event_id=EventId(f"e{sequence}"),
        run_id=run_id,
        occurred_at=NOW,
        sequence=sequence,
        objective=objective,
    )


def _lifecycle_stream() -> tuple[Event, ...]:
    """A realistic single-runtime stream: start → wait → grant → debit → stop."""
    proposal = ActionProposal(ActionId("a1"), "deploy", {"target": "ws"})
    return (
        _started(1),
        PlanCreated(
            event_id=EventId("e2"), run_id=RUN, occurred_at=NOW, sequence=2, plan="inspect"
        ),
        ActionProposed(
            event_id=EventId("e3"), run_id=RUN, occurred_at=NOW, sequence=3, proposal=proposal
        ),
        ApprovalRequested(
            event_id=EventId("e4"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=4,
            action_id=ActionId("a1"),
            reason="tool deploy requires operator approval (required)",
        ),
        OperatorInstruction(
            event_id=EventId("e5"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=5,
            instruction="looks safe",
        ),
        ApprovalGranted(
            event_id=EventId("e6"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=6,
            action_id=ActionId("a1"),
        ),
        BudgetDebited(
            event_id=EventId("e7"),
            run_id=RUN,
            occurred_at=NOW,
            sequence=7,
            usage=UsageDelta(cost_usd=0.25, input_tokens=10, output_tokens=5),
        ),
        RunStopped(
            event_id=EventId("e8"),
            run_id=RUN,
            occurred_at=LATER,
            sequence=8,
            reason=StopReason.SUCCESS_VERIFIED,
            summary="done",
        ),
    )


# --- status_after_event mapping --------------------------------------------------


def test_status_after_event_maps_run_stopped_by_reason() -> None:
    stopped = RunStopped(
        event_id=EventId("e9"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=9,
        reason=StopReason.BUDGET_EXHAUSTED,
        summary="out of budget",
    )
    assert status_after_event(stopped) is RunStatus.BUDGET_EXHAUSTED


def test_status_after_event_leaves_status_unchanged_for_projection_events() -> None:
    debited = BudgetDebited(
        event_id=EventId("e9"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=9,
        usage=UsageDelta(cost_usd=0.1),
    )
    instruction = OperatorInstruction(
        event_id=EventId("e10"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=10,
        instruction="steer",
    )
    merged = WorkerMerged(
        event_id=EventId("e11"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=11,
        worker_id=WorkerId("w1"),
        outcome=MergeOutcome.MERGED,
        revision="abc123",
        detail="merged",
    )
    model_turn = ModelTurnRecorded(
        event_id=EventId("e12"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=12,
        provider="ollama",
        model="devstral-small-2:latest",
        action_id=ActionId("a1"),
    )
    assert status_after_event(debited) is None
    assert status_after_event(instruction) is None
    assert status_after_event(merged) is None
    assert status_after_event(model_turn) is None


# --- fold conformance with replay -------------------------------------------------


def test_folded_index_matches_replay_for_a_realistic_stream() -> None:
    events = _lifecycle_stream()
    row: _RunIndexRow | None = None
    for event in events:
        row = fold_run_index_row(row, event)
    assert row is not None

    replayed = summarize_run(RUN, events)

    assert row.run_id == str(RUN)
    assert row.objective == replayed.objective == "repair"
    assert row.status == replayed.status.value == RunStatus.SUCCEEDED.value
    assert row.started_at == replayed.started_at.isoformat()
    assert row.last_occurred_at == replayed.last_occurred_at.isoformat()
    assert row.cost_usd == replayed.cost_usd
    assert row.stop_reason == StopReason.SUCCESS_VERIFIED.value


def test_folded_index_tracks_status_through_an_open_stream() -> None:
    events = _lifecycle_stream()[:4]  # up to ApprovalRequested
    row: _RunIndexRow | None = None
    for event in events:
        row = fold_run_index_row(row, event)
    assert row is not None
    assert row.status == RunStatus.WAITING_FOR_APPROVAL.value
    assert row.stop_reason is None
    assert row.cost_usd == 0.0


def test_folded_index_tracks_an_amended_objective() -> None:
    """An amending OperatorInstruction updates the index objective (replay parity)."""
    started = _started(1, objective="original")
    planned = PlanCreated(
        event_id=EventId("e2"), run_id=RUN, occurred_at=NOW, sequence=2, plan="inspect"
    )
    amended = OperatorInstruction(
        event_id=EventId("e3"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=3,
        instruction="amended objective",
        amends_objective=True,
    )
    steered = OperatorInstruction(
        event_id=EventId("e4"),
        run_id=RUN,
        occurred_at=NOW,
        sequence=4,
        instruction="non-amending steer",
    )
    events = (started, planned, amended, steered)
    row: _RunIndexRow | None = None
    for event in events:
        row = fold_run_index_row(row, event)
    assert row is not None

    replayed = summarize_run(RUN, events)

    assert row.objective == replayed.objective == "amended objective"


def test_summarize_run_rejects_an_empty_stream() -> None:
    with pytest.raises(ValueError, match="missing lifecycle timestamps"):
        summarize_run(RUN, ())


# --- InMemoryEventStore.list_runs --------------------------------------------------


def test_memory_store_lists_runs_newest_first() -> None:
    store = InMemoryEventStore()
    assert store.list_runs() == ()

    store.append(_started(1, run_id=RunId("older"), objective="first"), expected_version=0)
    store.append(
        RunStarted(
            event_id=EventId("e2"),
            run_id=RunId("newer"),
            occurred_at=LATER,
            sequence=1,
            objective="second",
        ),
        expected_version=0,
    )

    records = store.list_runs()

    assert [record.run_id for record in records] == [RunId("newer"), RunId("older")]
    newer = records[0]
    assert isinstance(newer, RunRecord)
    assert newer.objective == "second"
    assert newer.status is RunStatus.PLANNING
    assert newer.cost_usd == 0.0
    assert newer.stop_reason is None


def test_memory_store_index_matches_replay_projection() -> None:
    store = InMemoryEventStore()
    for index, event in enumerate(_lifecycle_stream()):
        store.append(event, expected_version=index)

    (record,) = store.list_runs()

    assert record == summarize_run(RUN, _lifecycle_stream())
