from __future__ import annotations

from typing import Protocol

from loopforge.domain.telemetry import TelemetryRecord, redact_record


class TelemetryEmissionError(RuntimeError):
    """Raised by telemetry adapters when emission fails.

    The fail-safe emission boundary catches this (and any other adapter
    exception) so observability failures can never corrupt run state, block
    the drive loop, or change run outcomes.
    """


class TelemetryPort(Protocol):
    """Non-authoritative observability sink.

    Telemetry is a projection of the authoritative event history, never a
    source of truth: adapters must not feed telemetry back into runtime
    decisions or state transitions. Records crossing this boundary have
    already been redacted; exporters must never see unredacted sensitive text.
    """

    def emit(self, record: TelemetryRecord) -> None: ...


class FailSafeTelemetry:
    """Fail-safe emission boundary: redacts, delegates, and never raises.

    Every record is redacted (code-owned, pre-emission) before it reaches the
    wrapped adapter. Any adapter failure is swallowed and counted so a broken
    telemetry pipeline cannot affect the authoritative runtime.
    """

    def __init__(self, sink: TelemetryPort) -> None:
        self._sink = sink
        self.dropped_records = 0

    def emit(self, record: TelemetryRecord) -> None:
        try:
            self._sink.emit(redact_record(record))
        except Exception:  # fail-safe boundary: telemetry must never corrupt run state
            self.dropped_records += 1
