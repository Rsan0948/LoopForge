from __future__ import annotations

from typing import Protocol

from loopforge.domain.events import Event
from loopforge.domain.types import RunId


class StreamVersionConflictError(RuntimeError):
    """Raised when a writer appends against a stale stream version."""

    def __init__(self, run_id: RunId, *, expected: int, actual: int) -> None:
        self.run_id = run_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stream {run_id} version conflict: expected {expected}, actual {actual}"
        )


class DuplicateEventError(RuntimeError):
    """Raised when an event identifier has already been persisted."""


class StateStorePort(Protocol):
    def append(self, event: Event, *, expected_version: int) -> int:
        """Append one event if the stream is exactly at expected_version.

        Returns the new stream version. Implementations must fail rather than
        silently reorder or overwrite concurrent writes.
        """
        ...

    def events_for(self, run_id: RunId) -> tuple[Event, ...]: ...

    def current_version(self, run_id: RunId) -> int: ...
