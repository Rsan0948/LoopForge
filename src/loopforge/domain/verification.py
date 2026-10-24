from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckOutcome:
    """One deterministic, code-owned verification check outcome."""

    name: str
    passed: bool
    detail: str
    executed: bool = True
    """False when the check never ran (sandbox/infra error, harness bug) —
    distinct from "ran and failed": the workspace was never measured."""
    partial: float | None = None
    """For a failed check only: the code-measured share of its sub-tests passing, in
    [0, 1). Lets the progress signal see repair inside one check; a failed check still
    never counts as passed."""

    def __post_init__(self) -> None:
        if self.partial is not None and (
            self.passed
            or isinstance(self.partial, bool)
            or not isinstance(self.partial, (int, float))  # pyright: ignore[reportUnnecessaryIsInstance]
            or not math.isfinite(self.partial)
            or not 0.0 <= self.partial < 1.0
        ):
            msg = "partial credit must be a finite fraction in [0, 1) on a failed check"
            raise ValueError(msg)
        if type(self.executed) is not bool:
            msg = "check outcome executed must be a bool"
            raise TypeError(msg)

        if self.passed and not self.executed:
            msg = "an unexecuted check cannot pass"
            raise ValueError(msg)
        if not isinstance(self.passed, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = "check outcome passed must be a bool"
            raise TypeError(msg_2)
        if not self.name.strip():
            msg = "check outcome name cannot be empty"
            raise ValueError(msg)
        if not isinstance(self.detail, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_3 = "check outcome detail must be a string"
            raise TypeError(msg_3)


@dataclass(frozen=True, slots=True, kw_only=True)
class CompositeVerification:
    """Deterministic composition of independent check outcomes."""

    passed: bool
    summary: str
    score: float
    inconclusive: bool = False
    """True when at least one check never executed (infra error): the score
    still counts it as not passed (fail-closed), but the verdict is flagged
    so operators and the console can tell "harness broken" apart from "code
    wrong"."""

    def __post_init__(self) -> None:
        if not math.isfinite(self.score) or not 0.0 <= self.score <= 1.0:
            msg = "composite verification score must be a finite fraction in [0, 1]"
            raise ValueError(msg)


def _outcome_verdict(outcome: CheckOutcome) -> str:
    if outcome.passed:
        return "passed"
    return "failed" if outcome.executed else "inconclusive"


def compose_check_outcomes(outcomes: tuple[CheckOutcome, ...]) -> CompositeVerification:
    """Compose independent check outcomes into one deterministic verdict.

    Composition is conjunctive: success requires every independent check to
    pass. The summary preserves the caller-supplied check order and the score
    is the passing fraction (failed checks contribute their partial credit, always
    below one check), so identical workspace state always yields an identical verdict.
    """
    if not outcomes:
        msg = "verification requires at least one check"
        raise ValueError(msg)
    passed_count = sum(1 for outcome in outcomes if outcome.passed)
    summary = "; ".join(
        f"{outcome.name}: {_outcome_verdict(outcome)} ({outcome.detail})" for outcome in outcomes
    )
    return CompositeVerification(
        passed=passed_count == len(outcomes),
        summary=summary,
        score=(
            passed_count
            + sum(o.partial for o in outcomes if not o.passed and o.partial is not None)
        )
        / len(outcomes),
        inconclusive=any(not outcome.executed for outcome in outcomes),
    )
