"""Code-owned, versioned local inference profiles; no runtime authority."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class OllamaProfile:
    name: str
    temperature: float
    top_p: float
    presence_penalty: float
    thinking: bool
    output_tokens: int
    preserve_thinking: bool = False
    context_tokens: int = 32768
    keep_alive: str = "5m"
    conversation_mode: Literal["history", "state"] = "history"
    thinking_policy: Literal["always", "on_evidence"] = "always"

    def options(self) -> dict[str, float | int]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": self.presence_penalty,
            "repeat_penalty": 1.0,
            "num_ctx": self.context_tokens,
            "num_predict": self.output_tokens,
        }


def profile_for(  # noqa: PLR0911, PLR0913 - explicit legacy and opt-in inference profiles
    role: Literal["coding", "review", "reporter"],
    *,
    candidate: bool = False,
    bounded_v2: bool = False,
    report_v3: bool = False,
    app_workload: bool = False,
    state_driven: bool = False,
    session: bool = False,
    native: bool = False,
) -> OllamaProfile:
    if native and role == "coding":
        # Append-only native tool chat (prefix KV cache reuse), reasoning enabled,
        # and Qwen's recommended thinking-mode sampling instead of greedy decoding.
        return OllamaProfile(
            "coding-native-v1",
            0.6,
            0.95,
            0.0,
            True,
            8192,
            context_tokens=98304,
            keep_alive="30m",
            conversation_mode="history",
            thinking_policy="on_evidence",
        )
    if session:
        return OllamaProfile(
            f"{role}-session-v1",
            0.0,
            0.95,
            0.0,
            False,
            4096 if role == "coding" else 2048,
            context_tokens=98304,
            conversation_mode="state",
        )
    if state_driven:
        return OllamaProfile(
            f"{role}-state-v1",
            0.0,
            0.95,
            0.0,
            False,
            4096 if role == "coding" else 2048,
            False,
            196608 if role == "reporter" else 65536,
            conversation_mode="state",
        )
    if app_workload:
        return OllamaProfile(
            f"{role}-app-v1",
            0.0,
            0.95,
            0.0,
            False,
            4096 if role == "coding" else 2048,
            False,
            196608 if role == "reporter" else 98304,
        )
    if bounded_v2 or report_v3:
        return OllamaProfile(
            f"{role}-bounded-v3" if report_v3 else f"{role}-bounded-v2",
            0.0,
            0.95,
            0.0,
            False,
            8192 if role == "coding" else 3072,
            False,
            98304 if role == "reporter" and report_v3 else 65536,
        )
    if role == "coding":
        return OllamaProfile(
            "coding-qwen-v1" if candidate else "coding-baseline-v1",
            0.6 if candidate else 0.0,
            0.95,
            0.0 if candidate else 1.5,
            True,
            8192,
            candidate,
        )
    return OllamaProfile(
        "reporter-bounded-v2" if role == "reporter" else "review-bounded-v1",
        0.0,
        0.95,
        0.0 if role == "reporter" else 1.5,
        False,
        2048 if role == "reporter" else 3072,
    )
