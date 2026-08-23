from __future__ import annotations

from typing import Protocol

from loopforge.domain.events import Event


class EventCodecPort(Protocol):
    """Stable serialization boundary for durable event stores."""

    def encode(self, event: Event) -> str: ...

    def decode(self, payload: str) -> Event: ...
