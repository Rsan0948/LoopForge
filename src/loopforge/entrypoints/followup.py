"""Deterministic follow-up report consolidation (PACS-014b).

A terminal run is a dead end unless its lessons travel. The follow-up
report is a bounded, code-owned digest of a finished run — stop reason,
final verification, tool usage, approvals, and the end-of-run workspace
inventory — folded into the NEXT session's objective so the model starts
from evidence instead of from scratch. The report is derived only from
the durable event stream (never from model-claimed state), and it is
text the operator reviews before pressing start: nothing auto-chains.
"""

from __future__ import annotations

from collections import Counter

from loopforge.domain.artifacts import ArtifactKind
from loopforge.domain.events import (
    ActionProposed,
    ApprovalGranted,
    ApprovalRejected,
    ArtifactRecorded,
    Event,
    RunStopped,
)
from loopforge.domain.state import RunState
from loopforge.domain.types import StopReason

REPORT_BUDGET = 2000


def consolidate_follow_up_report(run_id: str, events: tuple[Event, ...], state: RunState) -> str:
    """Build the bounded follow-up report for a terminal run.

    Deterministic and evidence-only: every line comes from the durable
    event stream or its replay projection. Bounded to ``_REPORT_BUDGET``
    characters so the folded objective stays a reasonable prompt size.
    """
    stopped = next((e for e in reversed(events) if isinstance(e, RunStopped)), None)
    stop_line = (
        f"ended {stopped.reason.value}: {stopped.summary}"
        if stopped is not None
        else f"ended in status {state.status.value}"
    )
    tool_counts = Counter(e.proposal.tool_name for e in events if isinstance(e, ActionProposed))
    tools = ", ".join(f"{name} x{count}" for name, count in sorted(tool_counts.items())) or "none"
    granted = sum(isinstance(e, ApprovalGranted) for e in events)
    rejected = sum(isinstance(e, ApprovalRejected) for e in events)
    verification = state.last_verification or "no verification ran"
    score = state.last_verification_score
    verification_line = f"{verification} (score {score:.2f})" if score is not None else verification
    succeeded = stopped is not None and stopped.reason is StopReason.SUCCESS_VERIFIED
    closing = (
        "The previous run succeeded; build on its verified state."
        if succeeded
        else (
            "Continue the task from this evidence: address the final verification "
            "failure first, and do not repeat approaches the previous run already "
            "exhausted."
        )
    )
    lines = [
        f"--- Follow-up report from run {run_id} ({stop_line}) ---",
        (
            f"iterations: {state.iteration}; cost: ${state.cost_usd:.4f}; "
            f"tokens: {state.input_tokens} in / {state.output_tokens} out"
        ),
        f"final verification: {verification_line}",
        f"tools used: {tools}",
        f"approvals: {granted} granted, {rejected} rejected",
        f"workspace at end: {_workspace_inventory(events)}",
        closing,
    ]
    report = "\n".join(lines)
    if len(report) > REPORT_BUDGET:
        report = report[: REPORT_BUDGET - 3] + "..."
    return report


def _workspace_inventory(events: tuple[Event, ...]) -> str:
    """Changed/untracked files from the last workspace-snapshot artifact."""
    snapshot = next(
        (
            e
            for e in reversed(events)
            if isinstance(e, ArtifactRecorded) and e.kind is ArtifactKind.WORKSPACE_SNAPSHOT
        ),
        None,
    )
    if snapshot is None:
        return "no workspace snapshot recorded"
    changed = untracked = "-"
    for line in snapshot.content.splitlines():
        if line.startswith("changed_files="):
            changed = line.removeprefix("changed_files=")
        elif line.startswith("untracked_files="):
            untracked = line.removeprefix("untracked_files=")
        elif not line:
            break
    return f"changed=[{changed}] untracked=[{untracked}]"
