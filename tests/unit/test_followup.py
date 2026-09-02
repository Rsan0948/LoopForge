"""Pins for the deterministic follow-up report (PACS-014b)."""

from datetime import UTC, datetime

from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import ArtifactKind
from loopforge.domain.events import (
    ActionProposed,
    ApprovalGranted,
    ApprovalRejected,
    ArtifactRecorded,
    RunStopped,
)
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, EventId, RunId, RunStatus, StopReason
from loopforge.entrypoints.followup import REPORT_BUDGET, consolidate_follow_up_report

NOW = datetime(2026, 9, 2, tzinfo=UTC)
RUN_ID = RunId("run_source")


def _proposal(sequence: int, tool: str) -> ActionProposed:
    return ActionProposed(
        event_id=EventId(f"evt_{sequence}"),
        run_id=RUN_ID,
        occurred_at=NOW,
        sequence=sequence,
        proposal=ActionProposal(action_id=ActionId(f"a{sequence}"), tool_name=tool, arguments={}),
    )


def _terminal_state(**overrides: object) -> RunState:
    base = {
        "status": RunStatus.FAILED,
        "objective": "fix the checks",
        "iteration": 4,
        "last_verification": "command:tests: failed (exit_code=1)",
        "last_verification_passed": False,
        "last_verification_score": 0.0,
        "cost_usd": 0.25,
        "input_tokens": 1200,
        "output_tokens": 300,
    }
    return RunState(run_id=RUN_ID, **{**base, **overrides})  # pyright: ignore[reportArgumentType]


def test_report_carries_the_terminal_runs_evidence() -> None:
    events = (
        _proposal(1, "workspace_status"),
        _proposal(2, "read_file"),
        _proposal(3, "read_file"),
        ApprovalGranted(
            event_id=EventId("evt_4"),
            run_id=RUN_ID,
            occurred_at=NOW,
            sequence=4,
            action_id=ActionId("a4"),
        ),
        ApprovalRejected(
            event_id=EventId("evt_5"),
            run_id=RUN_ID,
            occurred_at=NOW,
            sequence=5,
            action_id=ActionId("a5"),
            reason="no",
        ),
        ArtifactRecorded(
            event_id=EventId("evt_6"),
            run_id=RUN_ID,
            occurred_at=NOW,
            sequence=6,
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="workspace:server-session",
            content=(
                "workspace_id=server-session\n"
                "base_revision=abc123\n"
                "changed_files=blackjack/cards.py,blackjack/game.py\n"
                "untracked_files=-\n"
                "\n"
                "diff --git a/blackjack/cards.py b/blackjack/cards.py\n"
            ),
        ),
        RunStopped(
            event_id=EventId("evt_7"),
            run_id=RUN_ID,
            occurred_at=NOW,
            sequence=7,
            reason=StopReason.STALLED,
            summary="STOP_STALLED_NO_PROGRESS",
        ),
    )

    report = consolidate_follow_up_report("run_source", events, _terminal_state())

    assert "run_source" in report
    assert "stalled: STOP_STALLED_NO_PROGRESS" in report
    assert "iterations: 4" in report
    assert "command:tests: failed (exit_code=1) (score 0.00)" in report
    assert "read_file x2" in report
    assert "workspace_status x1" in report
    assert "1 granted, 1 rejected" in report
    assert "changed=[blackjack/cards.py,blackjack/game.py] untracked=[-]" in report


def test_report_without_snapshot_or_stop_event_stays_truthful() -> None:
    report = consolidate_follow_up_report(
        "run_source",
        (_proposal(1, "read_file"),),
        _terminal_state(status=RunStatus.CANCELLED, last_verification=None),
    )

    assert "ended in status cancelled" in report
    assert "no verification ran" in report
    assert "no workspace snapshot recorded" in report


def test_report_is_bounded() -> None:
    state = _terminal_state(last_verification="x" * 10_000)
    report = consolidate_follow_up_report("run_source", (), state)
    assert len(report) <= REPORT_BUDGET
