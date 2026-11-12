"""Provider-independent structured report generation; authority stays with the caller."""

from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.types import UsageDelta


@dataclass(frozen=True, slots=True)
class ReportTurn:
    content: str
    usage: UsageDelta


class ReportingPort(Protocol):
    def generate(
        self, packet: str, schema: dict[str, object], *, objective: str = ""
    ) -> ReportTurn: ...
