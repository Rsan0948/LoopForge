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


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationProgress:
    """Comparable evidence under a frozen, code-owned measurement basis."""

    basis: str
    checks: tuple[str, ...]
    passed: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.basis, str) or not self.basis.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "verification progress basis must be nonempty"
            raise ValueError(msg)
        for values in (self.checks, self.passed):
            if (
                type(values) is not tuple
                or any(
                    not isinstance(value, str) or not value.strip()  # pyright: ignore[reportUnnecessaryIsInstance]
                    for value in values
                )
                or len(set(values)) != len(values)
            ):
                msg = "verification progress IDs must be unique immutable strings"
                raise TypeError(msg)
        if not self.checks or not set(self.passed) <= set(self.checks):
            msg = "verification progress passes must belong to its check universe"
            raise ValueError(msg)

    def comparable_to(self, other: VerificationProgress) -> bool:
        return self.basis == other.basis and set(self.checks) == set(other.checks)

    def dominates(self, other: VerificationProgress) -> bool:
        return self.comparable_to(other) and set(self.passed) > set(other.passed)


def validate_verification_evidence(  # noqa: PLR0913 - one shared verification envelope contract
    outcomes: tuple[CheckOutcome, ...],
    progress: VerificationProgress | None,
    *,
    passed: bool,
    summary: str,
    task_complete: bool = True,
    inconclusive: bool = False,
    evidence_identity: str | None = None,
) -> None:
    if not isinstance(summary, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = "verification summary must be a string"
        raise TypeError(msg)
    if evidence_identity is not None and (
        not isinstance(evidence_identity, str)  # pyright: ignore[reportUnnecessaryIsInstance]
        or not evidence_identity.strip()
        or len(evidence_identity) > 512
    ):
        msg = "verification evidence identity must be a bounded nonempty string"
        raise ValueError(msg)
    if type(outcomes) is not tuple or any(
        not isinstance(outcome, CheckOutcome)  # pyright: ignore[reportUnnecessaryIsInstance]
        for outcome in outcomes
    ):
        msg = "verification outcomes must be immutable typed checks"
        raise TypeError(msg)
    if len({outcome.name for outcome in outcomes}) != len(outcomes):
        msg = "verification check names must be unique"
        raise ValueError(msg)
    if passed and any(not o.passed or not o.executed for o in outcomes):
        msg = "passing verification cannot contradict check evidence"
        raise ValueError(msg)
    if progress is not None:
        if not isinstance(progress, VerificationProgress):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "verification progress must be typed"
            raise TypeError(msg)
        measured = {o.name for o in outcomes if o.passed and o.executed}
        if set(progress.passed) != measured or not {o.name for o in outcomes} <= set(
            progress.checks
        ):
            msg = "verification progress must match measured outcomes"
            raise ValueError(msg)
        if passed and task_complete and set(progress.passed) != set(progress.checks):
            msg = "complete verification must pass its declared check universe"
            raise ValueError(msg)
        if inconclusive:
            msg = "inconclusive verification cannot carry comparable progress"
            raise ValueError(msg)
