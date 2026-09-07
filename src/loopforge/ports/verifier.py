from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.state import RunState


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

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "verification passed must be a bool"
            raise TypeError(msg)
        if not isinstance(self.summary, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = "verification summary must be a string"
            raise TypeError(msg_2)
        if self.score is not None and (
            not math.isfinite(self.score) or not 0.0 <= self.score <= 1.0
        ):
            msg_3 = "verification score must be a finite fraction in [0, 1]"
            raise ValueError(msg_3)


class VerifierPort(Protocol):
    def verify(self, state: RunState) -> VerificationResult: ...
