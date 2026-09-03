"""Operator maintenance commands that act on streams too broken to replay.

The runtime's control commands all replay the authoritative stream before
writing (they must know the current status to pick a legal transition). A
stream that no longer replays — corruption, a bug-compromised event — makes
every one of those commands unavailable at exactly the moment the operator
needs an escape hatch. The commands here deliberately read nothing but the
stream version (a pure ``MAX(sequence)``) and compare-and-append, so they
keep working on streams the reducer can no longer validate (PACS-015).

The operator is the liveness check: nothing here can tell a wedged driver
from a slow one (no cross-process liveness marker exists). The mechanical
guarantees are narrower and honest — the release is itself race-safe (a
concurrent append turns into ``StreamVersionConflictError``, failing closed),
and it never rewrites history: it appends exactly one terminal record.
"""

from __future__ import annotations

from uuid import uuid4

from loopforge.application.runtime import UnknownRunError
from loopforge.domain.events import RunStopped
from loopforge.domain.types import EventId, RunId, StopReason
from loopforge.ports.clock import ClockPort
from loopforge.ports.state_store import StateStorePort


def force_stop_run(store: StateStorePort, run_id: RunId, *, summary: str, clock: ClockPort) -> int:
    """Append ``RunStopped(CANCELLED)`` without replaying the stream.

    Unlike ``Runtime.cancel`` — which replays first and is therefore useless
    on a corrupted stream — this reads only ``current_version`` and
    compare-and-appends the terminal record. A racing writer (a driver in
    another process, say) turns the append into ``StreamVersionConflictError``:
    the release fails closed rather than silently reordering history.

    Terminality is deliberately NOT checked here: checking it requires a
    replay, which is exactly what a corrupted stream denies. Callers on
    healthy streams (``SessionManager.force_release``) deny already-terminal
    runs before delegating. Returns the new stream version; raises
    ``UnknownRunError`` when the stream is empty.
    """
    version = store.current_version(run_id)
    if version == 0:
        msg = f"no persisted run: {run_id}"
        raise UnknownRunError(msg)
    event = RunStopped(
        event_id=EventId(f"evt_{uuid4().hex[:16]}"),
        run_id=run_id,
        occurred_at=clock.now(),
        sequence=version + 1,
        reason=StopReason.CANCELLED,
        summary=summary,
    )
    return store.append(event, expected_version=version)
