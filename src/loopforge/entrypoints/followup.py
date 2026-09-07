"""Deterministic follow-up report consolidation (PACS-014b).

A terminal run is a dead end unless its lessons travel. The follow-up
report is a bounded, code-owned digest of a finished run — stop reason,
verification history, tool errors, tool usage, approvals, the last patch,
and the end-of-run workspace inventory — folded into the NEXT session's
objective so the model starts from evidence instead of from scratch. The
report is derived only from the durable event stream (never from
model-claimed state), and it is text the operator reviews before pressing
start: nothing auto-chains.
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
    ToolFailed,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.state import RunState
from loopforge.domain.types import StopReason

REPORT_BUDGET = 3000
_LINE_BUDGET = 200
_HISTORY_ENTRIES = 4
_TOOL_ERROR_ENTRIES = 3
_PATCH_BUDGET = 700


def _clip(text: str, budget: int = _LINE_BUDGET) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= budget else collapsed[: budget - 3] + "..."


def consolidate_follow_up_report(run_id: str, events: tuple[Event, ...], state: RunState) -> str:
    """Build the bounded follow-up report for a terminal run.

    Deterministic and evidence-only: every line comes from the durable
    event stream or its replay projection. Bounded to ``REPORT_BUDGET``
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
    last_failed = next((e for e in reversed(events) if isinstance(e, VerificationFailed)), None)
    inconclusive = last_failed is not None and last_failed.inconclusive
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
        _verification_history(events),
        _tool_errors(events),
        f"tools used: {tools}",
        f"approvals: {granted} granted, {rejected} rejected",
        f"workspace at end: {_workspace_inventory(events)}",
        _last_patch(events),
    ]
    if inconclusive:
        lines.append(
            "note: the final verdict reflects checks that could not execute "
            "(infrastructure error), not measured code — fix the harness/sandbox "
            "configuration before spending more model turns."
        )
    lines.append(closing)
    report = "\n".join(lines)
    if len(report) > REPORT_BUDGET:
        report = report[: REPORT_BUDGET - 3] + "..."
    return report


def _verification_history(events: tuple[Event, ...]) -> str:
    """Recent verification verdicts, oldest first, honestly marked."""
    verdicts: list[str] = []
    for event in events:
        if isinstance(event, VerificationFailed):
            entry = event.summary
            if event.score is not None:
                entry += f" (score {event.score:.2f})"
            if event.inconclusive:
                entry += " [checks could not execute]"
            verdicts.append(entry)
        elif isinstance(event, VerificationPassed):
            verdicts.append(f"{event.summary} (passed)")
    if not verdicts:
        return "verification history: none recorded"
    recent = verdicts[-_HISTORY_ENTRIES:]
    omitted = len(verdicts) - len(recent)
    header = "verification history"
    if omitted:
        header += f" (last {len(recent)} of {len(verdicts)})"
    body = "; ".join(_clip(entry) for entry in recent)
    return f"{header}: {body}"


def _tool_errors(events: tuple[Event, ...]) -> str:
    """Most recent tool failures with their error texts."""
    failures = [event for event in events if isinstance(event, ToolFailed)]
    if not failures:
        return "tool errors: none recorded"
    recent = failures[-_TOOL_ERROR_ENTRIES:]
    omitted = len(failures) - len(recent)
    header = "tool errors"
    if omitted:
        header += f" (last {len(recent)} of {len(failures)})"
    body = "; ".join(f"{event.error_code}: {_clip(event.error_message)}" for event in recent)
    return f"{header}: {body}"


def _last_patch(events: tuple[Event, ...]) -> str:
    """The diff body from the last workspace snapshot, bounded."""
    snapshot = _last_snapshot(events)
    if snapshot is None:
        return "last patch: no workspace snapshot recorded"
    _, _, diff = snapshot.content.partition("\n\n")
    diff = diff.strip()
    if not diff:
        return "last patch: none recorded"
    if len(diff) > _PATCH_BUDGET:
        diff = diff[: _PATCH_BUDGET - 3] + "..."
    return f"last patch (end-of-run snapshot):\n{diff}"


def _last_snapshot(events: tuple[Event, ...]) -> ArtifactRecorded | None:
    return next(
        (
            e
            for e in reversed(events)
            if isinstance(e, ArtifactRecorded) and e.kind is ArtifactKind.WORKSPACE_SNAPSHOT
        ),
        None,
    )


def _workspace_inventory(events: tuple[Event, ...]) -> str:
    """Changed/untracked files from the last workspace-snapshot artifact."""
    snapshot = _last_snapshot(events)
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
