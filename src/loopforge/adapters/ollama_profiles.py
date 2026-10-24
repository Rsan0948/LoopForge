"""Code-owned, versioned local inference profiles; no runtime authority."""

from dataclasses import dataclass
from typing import Final, Literal


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
    history_replay: Literal["bounded", "exact"] = "bounded"
    # Delimited multi-line observations and repeated-failure fingerprints (observation_format).
    observation_format: Literal["plain", "delimited"] = "plain"
    # Code-owned read_file window in characters; 3000 keeps legacy paging.
    read_page_chars: int = 3000
    # Prompt-token usage budget kept by checkpoint compaction; None disables it.
    working_budget_tokens: int | None = None

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


# Usage budget when the coding worker runs on MLX (CampaignConfig.engine). Measured decode
# with a 256-token output: 41.3 / 43.8 / 37.4 tok/s at 40.1K / 48.6K / 57.2K prompt
# tokens (Ollama f16: 34.1 / 37.4 / 29.8). A 48K budget keeps MLX above 40 tok/s and
# halves compaction frequency against 32K. Run: .loopforge/mlx-lab/runs/depth-deep-0811.
MLX_WORKING_BUDGET_TOKENS: Final = 49152


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
    model: str = "",
) -> OllamaProfile:
    if native and role == "coding":
        # Append-only native tool chat (prefix KV cache reuse), reasoning enabled,
        # and Qwen's recommended thinking-mode sampling instead of greedy decoding.
        if model == "qwen3.6:35b-a3b":
            # Vendor sampling for this MoE model. Without presence_penalty its
            # thinking cycles through the same reasoning; measured on a captured
            # 25K-token request: 720 output tokens with 1.5 versus a long, looping
            # trace with 0.0. The 32K allowance keeps long reasoning from being
            # cut off before the action; 457 tok/s prefill, ~15 tok/s decode on M1 Max.
            # Hybrid model: a prompt that rewrites earlier turns cannot reuse the
            # recurrent cache. Ollama's qwen3.5 renderer renders every earlier
            # assistant turn by the current request's think flag, so the flag stays
            # constant, and thinking/content replay exactly as generated.
            # v3 (2026-10-23 roadmap): delimited observations with repeated-failure
            # fingerprints, whole-file reads up to 12K characters, and a 16K output cap
            # (the largest single output across 328 lab calls was 15.5K tokens; p99 9.3K).
            # The 131K allocation is kept: measured decode cost is identical at 32K and
            # 131K allocation for the same depth, and Qwen advises >=128K for thinking.
            return OllamaProfile(
                "coding-native-a3b-v3",
                1.0,
                0.95,
                1.5,
                True,
                16384,
                preserve_thinking=True,
                context_tokens=131072,
                keep_alive="30m",
                conversation_mode="history",
                thinking_policy="always",
                history_replay="exact",
                observation_format="delimited",
                read_page_chars=12000,
                # Usage budget (user target: stay under 32K). Measured decode with an f16
                # KV cache stays at or above 36 tok/s through 30K on Ollama and about 48 on MLX.
                working_budget_tokens=32768,
            )
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
