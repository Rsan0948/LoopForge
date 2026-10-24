from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.state import RunState
from loopforge.domain.verification import CheckOutcome


class VerifierContractError(TypeError):
    """Raised when a verifier violates the runtime response contract."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    summary: str
    score: float | None = None
    inconclusive: bool = False
    """True when at least one check never executed (infra error, not a code
    verdict). Fail-closed: inconclusive results are never ``passed``."""

    repeat: bool = False
    """True when the verdict is re-reported for an unchanged, already verified
    candidate; the run records it without counting a new attempt."""

    task_complete: bool = True
    """Code-owned verdict that all workload obligations are satisfied.

    False records a passing checkpoint and continues the run. Existing
    verifiers retain their whole-task semantics through the default.
    """

    outcomes: tuple[CheckOutcome, ...] = ()

    def __post_init__(self) -> None:
        if type(self.outcomes) is not tuple or any(
            not isinstance(outcome, CheckOutcome)  # pyright: ignore[reportUnnecessaryIsInstance]
            for outcome in self.outcomes
        ):
            msg = "verification outcomes must be immutable typed checks"
            raise TypeError(msg)
        if len({outcome.name for outcome in self.outcomes}) != len(self.outcomes):
            msg = "verification check names must be unique"
            raise ValueError(msg)
        if self.passed and any(
            not outcome.passed or not outcome.executed for outcome in self.outcomes
        ):
            msg = "passing verification cannot contradict check evidence"
            raise ValueError(msg)

        if not isinstance(self.task_complete, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "verification task_complete must be a bool"
            raise TypeError(msg)
        if not isinstance(self.inconclusive, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "verification inconclusive must be a bool"
            raise TypeError(msg)
        if self.passed and self.inconclusive:
            msg = "inconclusive verification cannot pass"
            raise ValueError(msg)
        if not isinstance(self.passed, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "verification passed must be a bool"
            raise TypeError(msg)
        if not isinstance(self.summary, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = "verification summary must be a string"
            raise TypeError(msg_2)
        if self.score is not None and (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))  # pyright: ignore[reportUnnecessaryIsInstance]
            or not 0.0 <= self.score <= 1.0
            or not math.isfinite(self.score)
        ):
            msg_3 = "verification score must be a finite fraction in [0, 1]"
            raise ValueError(msg_3)


class VerifierPort(Protocol):
    def verify(self, state: RunState) -> VerificationResult: ...
