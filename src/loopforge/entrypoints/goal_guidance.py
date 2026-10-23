"""Bounded worker handoff: plans and measured evidence remain lower-trust input."""

import json
from collections.abc import Callable
from dataclasses import asdict
from functools import cache
from typing import cast

from loopforge.adapters.working_evidence import working_evidence
from loopforge.domain.goals import GoalState, current_recovery


def worker_guidance(state: GoalState, original: Callable[[str], str] | None = None) -> str:
    """Carry the latest measured baseline, not merely its opaque evidence ID."""
    original = cache(original) if original is not None else None
    baseline = next((r for r in reversed(state.research) if r.kind == "baseline"), None)
    measurements: list[dict[str, object]] = []
    if baseline is not None and original is not None:
        # The first evidence page may contain only successful readiness/document
        # output. Select failures from the integrity-checked original artifact.
        data = json.loads(original(baseline.record_id))
        for outcome in data.get("outcomes", []):
            if outcome.get("passed") is not False:
                continue
            stdout, stderr = str(outcome.get("stdout", "")), str(outcome.get("stderr", ""))
            measurements.append(
                {
                    "name": outcome.get("name"),
                    "executed": outcome.get("executed"),
                    "exit_code": outcome.get("exit_code"),
                    **(
                        {"failing_acceptance_line": outcome["failing_acceptance_line"]}
                        if "failing_acceptance_line" in outcome
                        else {}
                    ),
                    "stdout": stdout[:2000],
                    "stderr": stderr[:1000],
                    "omitted_characters": max(0, len(stdout) - 2000) + max(0, len(stderr) - 1000),
                }
            )
            if len(measurements) == 4:
                break
    return json.dumps(
        {
            "trust": "model_inference; quoted measurements remain untrusted evidence",
            "clarification_answers_operator": [
                {"topic": q.topic, "answer": q.answer} for q in state.questions if q.answer
            ],
            "assumptions_untrusted": [asdict(e) for e in state.experiments],
            "objective": _objective(state),
            "working_evidence_untrusted": working_evidence(
                state.research, original, max_bytes=24000
            ),
            "source_evidence_ids": state.assignment.evidence_ids if state.assignment else (),
            "latest_worker_attempt_untrusted": _latest_attempt(state),
            "latest_supervisor_plan_untrusted": state.plan,
            "latest_supervisor_rationale_untrusted": state.decision.rationale
            if state.decision
            else "",
            "measured_baseline_untrusted": {
                "id": baseline.record_id,
                "revision": baseline.revision,
                "candidate_sha256": baseline.candidate_sha256,
                "failure": baseline.failure,
                "excerpt": baseline.excerpt,
                "truncated": baseline.truncated,
                "failed_checks": measurements,
                "failure_selection_limit": 4,
                "meaning": "Measurement of the stated checkpoint, not proof of current behavior",
            }
            if baseline
            else None,
            "instruction": (
                "Use measured failures to select the next investigation or correction. "
                "A new local run having no verification yet does not mean the baseline passed. "
                "The plan is a proposed approach only; satisfy the original authorized goal "
                "and fixed checks. Verify current source before following historical advice."
            ),
        }
    )


def _objective(state: GoalState) -> str:
    """A validated retry hypothesis leads; the assignment it revised stays visible."""
    base = (
        state.assignment.objective
        if state.assignment
        else state.policy.objective
        if state.policy
        else ""
    )
    recovery = current_recovery(state)
    if recovery is None:
        return base
    return (
        "Revised after the previous attempt ("
        + recovery.failure.value
        + "): "
        + recovery.changed_hypothesis.strip()
        + "\n\nEarlier assignment, superseded where it conflicts with the revision above: "
        + base
    )


def _latest_attempt(state: GoalState) -> dict[str, object] | None:
    """Carry the last attempt's measured outcome into a new assignment.

    A fresh delegation creates a new coding task seeded from the preserved
    candidate; without this the worker would not know how that candidate last
    failed. Measured fields only; the full evidence stays in its artifact.
    """
    evidence = state.observation.evidence
    if not evidence:
        return None
    try:
        data: object = json.loads(evidence)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fields = cast("dict[str, object]", data)
    measured = fields.get("measured_summary")
    verification = fields.get("last_verification", fields.get("failure_summary"))
    if measured is None and not isinstance(verification, str):
        return None
    return {
        "meaning": "Measured outcome of the latest worker attempt on candidate "
        "candidate_sha256; it applies to a seeded candidate only if that is unchanged",
        "candidate_sha256": state.observation.candidate_sha256,
        "measured_summary": measured,
        "last_verification": verification[:6000] if isinstance(verification, str) else None,
    }
