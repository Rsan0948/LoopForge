from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.state import RunState


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    summary: str
    score: float | None = None


class VerifierPort(Protocol):
    def verify(self, state: RunState) -> VerificationResult: ...
