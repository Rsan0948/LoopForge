"""The supervisor receives code-derived worker facts, not only text to interpret."""

from datetime import UTC, datetime

from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import ActionProposed
from loopforge.domain.types import ActionId, EventId, RunId
from loopforge.entrypoints.goal_worker import measured_summary


def _proposed(index: int, tool: str) -> ActionProposed:
    return ActionProposed(
        event_id=EventId(f"e{index}"),
        run_id=RunId("run_x"),
        occurred_at=datetime.now(UTC),
        sequence=index,
        proposal=ActionProposal(ActionId(f"a{index}"), tool, {}),
    )


def test_summary_reports_actual_files_checks_and_actions() -> None:
    detail: dict[str, object] = {
        "run_id": "run_x",
        "run_status": "failed",
        "last_verification": "command:check_documents: passed (exit_code=0); "
        "command:check_project: failed (exit_code=1; stdout=...); "
        "patch_constraints: passed (files changed: app/server.py)",
    }
    events = [_proposed(1, "read_file"), _proposed(2, "read_file"), _proposed(3, "apply_edits")]
    summary = measured_summary(detail, ["app/server.py"], events)
    assert summary["files_changed_vs_assignment_baseline"] == ["app/server.py"]
    assert summary["checks"] == {
        "check_documents": "passed",
        "check_project": "failed",
        "patch_constraints": "passed",
    }
    assert summary["worker_actions"] == {"reads": 2, "edits": 1}
    assert "not model claims" in str(summary["trust"])


def test_summary_without_verification_says_so_rather_than_implying_success() -> None:
    summary = measured_summary({"run_id": "run_x"}, [], [])
    assert summary["checks"] == "no verification recorded for this attempt"
    assert summary["worker_actions"] == {}
    assert summary["files_changed_vs_assignment_baseline"] == []
