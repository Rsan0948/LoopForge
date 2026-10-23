"""Offline: how much of each prompt survives as an exact prefix of the next one.

Uses Ollama's own qwen3.5 renderer (Go oracle) on a recorded comparison trial, rendering
consecutive requests (a) as they were sent and (b) as the exact-replay profile builds them:
constant think flag, assistant thinking and content replayed verbatim. A hybrid model can
only reuse cache across an exact prefix, so (b) should keep ~100% and (a) shows the loss.
No inference.

Usage: .venv/bin/python scripts/verify_prompt_prefix_stability.py TRIAL_DIR
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORACLE = ROOT / ".loopforge/mlx-lab/renderer-oracle/renderer-oracle"


def render(native: dict[str, object]) -> str:
    out = subprocess.run(
        [str(ORACLE)], input=json.dumps(native), capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)["prompt"]


def exact_replay(requests: list[dict], responses: list[dict]) -> list[dict]:
    """Rebuild each request as the exact-replay profile would: think on, turns verbatim."""
    rebuilt = []
    for native in requests:
        request = copy.deepcopy(native)
        request["think"] = True
        turn = 0
        for message in request["messages"]:
            if message["role"] == "assistant" and turn < len(responses):
                generated = responses[turn]["message"]
                message["thinking"] = generated.get("thinking", "")
                message["content"] = generated.get("content", "")
                turn += 1
        # Exact replay folds runtime updates into the preceding tool response.
        merged: list[dict] = []
        for message in request["messages"]:
            content = str(message.get("content", ""))
            if (
                message["role"] == "user"
                and content.startswith("Runtime update:")
                and merged
                and merged[-1]["role"] == "tool"
            ):
                merged[-1]["content"] += "\n" + content
            else:
                merged.append(message)
        request["messages"] = merged
        rebuilt.append(request)
    return rebuilt


def common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    return next((i for i in range(n) if a[i] != b[i]), n)


def report(label: str, requests: list[dict]) -> float:
    prompts = [render(r) for r in requests]
    kept = []
    for i in range(len(prompts) - 1):
        k = common_prefix(prompts[i], prompts[i + 1])
        kept.append(k / len(prompts[i]))
    worst = min(kept) if kept else 1.0
    lost = sum(len(p) * (1 - f) for p, f in zip(prompts, kept, strict=False))
    print(
        f"{label:12} pairs={len(kept):3}  mean prefix kept={sum(kept) / len(kept):6.1%}  "
        f"worst={worst:6.1%}  chars to re-process={int(lost):,}"
    )
    return worst


def main() -> None:
    trial = Path(sys.argv[1])
    exchanges = sorted(trial.glob("exchange-*.json"))
    records = [json.loads(p.read_text()) for p in exchanges]
    records = [r for r in records if "response" in r]
    requests = [r["native_request"] for r in records]
    responses = [r["response"] for r in records]
    report("as sent", requests)
    worst = report("exact replay", exact_replay(requests, responses))
    sys.exit(0 if worst > 0.99 else 1)


if __name__ == "__main__":
    main()
