from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class SleeperPort(Protocol):
    def sleep(self, seconds: float) -> None: ...
