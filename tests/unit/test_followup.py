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
    ToolFailed,
    VerificationFailed,
)
from loopforge.domain.reliability import ToolFailureClass
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


def test_report_carries_verification_history_tool_errors_and_last_patch() -> None:
    events = (
        VerificationFailed(
            event_id=EventId("evt_1"),
            run_id=RUN_ID,
            occurred_at=NOW,
            sequence=1,
            summary="command:tests: failed (exit_code=1)",
            score=0.0,
        ),
        VerificationFailed(
            event_id=EventId("evt_2"),
            run_id=RUN_ID,
            occurred_at=NOW,
            sequence=2,
            summary="command:tests: failed (sandbox error: launcher died)",
            score=0.5,
            inconclusive=True,
        ),
        ToolFailed(
            event_id=EventId("evt_3"),
            run_id=RUN_ID,
            occurred_at=NOW,
            sequence=3,
            action_id=ActionId("a3"),
            attempt=1,
            error_code="WORKSPACE_ERROR",
            error_message="git checkout failed: pathspec did not match",
            failure_class=ToolFailureClass.PERMANENT,
        ),
        ArtifactRecorded(
            event_id=EventId("evt_4"),
            run_id=RUN_ID,
            occurred_at=NOW,
            sequence=4,
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="workspace:server-session",
            content=(
                "changed_files=game.py\nuntracked_files=-\n\n"
                "diff --git a/game.py b/game.py\n+hit_me = True\n"
            ),
        ),
        RunStopped(
            event_id=EventId("evt_5"),
            run_id=RUN_ID,
            occurred_at=NOW,
            sequence=5,
            reason=StopReason.FAILURE,
            summary="MODEL_INVALID_RESPONSE: 0 tool actions",
        ),
    )

    report = consolidate_follow_up_report("run_source", events, _terminal_state())

    assert "verification history: " in report
    assert "exit_code=1" in report
    assert "sandbox error: launcher died" in report
    assert "[checks could not execute]" in report
    assert "(score 0.50)" in report
    assert "tool errors: WORKSPACE_ERROR: git checkout failed" in report
    assert "diff --git a/game.py b/game.py" in report
    assert "+hit_me = True" in report
    # The last verification was inconclusive: the report steers the operator
    # at the harness, not the model.
    assert "infrastructure error" in report


def test_report_marks_absent_history_and_patch_honestly() -> None:
    report = consolidate_follow_up_report(
        "run_source", (_proposal(1, "read_file"),), _terminal_state()
    )
    assert "verification history: none recorded" in report
    assert "tool errors: none recorded" in report
    assert "last patch: no workspace snapshot recorded" in report
