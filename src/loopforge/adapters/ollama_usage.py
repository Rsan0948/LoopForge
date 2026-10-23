"""Parse known consumption independently of invalid provider message content."""

import math
from dataclasses import asdict
from datetime import UTC, datetime
from threading import Lock
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loopforge.domain.admission import ContextAdmission
from loopforge.domain.types import UsageDelta


class ProviderRequestTracker:
    """Read-only process liveness; never authoritative run state."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.status: dict[str, object] = {"phase": "idle"}
        self.history: list[dict[str, object]] = []

    def begin(self, timeout: float, kind: str) -> None:
        with self.lock:
            self.status = {
                "phase": "waiting_for_provider",
                "kind": kind,
                "started_at": datetime.now(UTC).isoformat(),
                "timeout_seconds": timeout,
            }

    def end(self) -> None:
        with self.lock:
            self.status = {
                **self.status,
                "phase": "idle",
                "finished_at": datetime.now(UTC).isoformat(),
            }

    def note(self, reason: str) -> None:
        with self.lock:
            self.history.append({"mode_change": reason, "at": datetime.now(UTC).isoformat()})
            self.history = self.history[-256:]

    def record_admission(self, admission: ContextAdmission) -> None:
        with self.lock:
            self.history.append(
                {"context_admission": asdict(admission), "admitted": admission.admitted}
            )
            self.history = self.history[-256:]

    def record(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        parsed = cast("dict[str, object]", data)
        fields = {
            "model",
            "load_duration",
            "prompt_eval_duration",
            "eval_duration",
            "total_duration",
            "prompt_eval_count",
            "eval_count",
            "prompt_eval_cached_count",
        }
        metrics: dict[str, object] = {}
        for key in fields:
            value = parsed.get(key)
            if (key == "model" and isinstance(value, str)) or (type(value) is int and value >= 0):
                metrics[key] = value
        duration, count = metrics.get("eval_duration"), metrics.get("eval_count")
        if isinstance(duration, int) and duration > 0 and isinstance(count, int):
            rate = count * 1_000_000_000 / duration
            if math.isfinite(rate):
                metrics["decode_tokens_per_second"] = rate
        with self.lock:
            entry = {**self.status, "metrics": metrics}
            self.history.append(entry)
            self.history = self.history[-256:]
            self.status["metrics"] = metrics

    def observations(self) -> list[dict[str, object]]:
        with self.lock:
            return [dict(item) for item in self.history]

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return dict(self.status)


class ProviderUsage(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    prompt_eval_count: int = Field(ge=0)
    eval_count: int = Field(ge=0)


def known_usage(data: object) -> UsageDelta | None:
    try:
        usage = ProviderUsage.model_validate(data)
    except ValidationError:
        return None
    return UsageDelta(input_tokens=usage.prompt_eval_count, output_tokens=usage.eval_count)
