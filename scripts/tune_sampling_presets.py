"""Tuning: sampling presets on captured requests (output length, looping, tool choice).

Each captured request is replayed with every preset and several seeds. Reports output
tokens, the repeated-sentence share of the thinking (cycling), whether a tool call came
back, and which tool. No task execution; the user's Ollama service is used as configured.

Usage: .venv/bin/python scripts/tune_sampling_presets.py TRIAL_DIR OUTPUT_JSON [SEEDS]
"""

import json
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

from loopforge.adapters.model_lease import ModelLease
from scripts import run_engine_comparison as cmp

CALLS = (3, 8, 14, 16, 18, 22)
CAP = 8192
PRESETS = {
    "general T1.0 pp1.5": {"temperature": 1.0, "presence_penalty": 1.5},
    "precise T0.6 pp0.0": {"temperature": 0.6, "presence_penalty": 0.0},
    "hybrid T0.6 pp1.5": {"temperature": 0.6, "presence_penalty": 1.5},
}


def repeated_share(text: str) -> float:
    sentences = [s.strip().lower() for s in re.split(r"[.!?\n]+", text) if len(s.strip()) > 25]
    if not sentences:
        return 0.0
    return 1 - len(set(sentences)) / len(sentences)


def main() -> None:
    trial, out = Path(sys.argv[1]), Path(sys.argv[2])
    seeds = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    natives = [
        json.loads((trial / f"exchange-{c:03}.json").read_text())["native_request"] for c in CALLS
    ]
    rows: list[dict[str, object]] = []
    lease = ModelLease()
    lease.acquire("sampling-preset-tuning")
    try:
        cmp.unload_ollama()
        cmp.warm_ollama()
        for name, preset in PRESETS.items():
            for call, native in zip(CALLS, natives, strict=True):
                for seed in range(seeds):
                    options = {**native["options"], **preset, "num_predict": CAP, "seed": seed}
                    started = time.monotonic()
                    data = (
                        httpx.post(
                            f"{cmp.OLLAMA}/api/chat",
                            json={**native, "think": True, "stream": False, "options": options},
                            timeout=1800,
                            trust_env=False,
                        )
                        .raise_for_status()
                        .json()
                    )
                    message = data["message"]
                    tools = message.get("tool_calls") or []
                    row = {
                        "preset": name,
                        "call": call,
                        "seed": seed,
                        "output_tokens": data["eval_count"],
                        "hit_cap": data["eval_count"] >= CAP,
                        "repeated_share": round(repeated_share(message.get("thinking", "")), 3),
                        "tool": tools[0]["function"]["name"] if tools else None,
                        "wall_seconds": round(time.monotonic() - started, 1),
                        "decode_tok_s": round(
                            data["eval_count"] / (data["eval_duration"] / 1e9), 1
                        ),
                    }
                    rows.append(row)
                    print(json.dumps(row), flush=True)
                    out.write_text(json.dumps(rows, indent=2))
    finally:
        cmp.unload_ollama()
        lease.release()
    for name in PRESETS:
        mine = [r for r in rows if r["preset"] == name]
        outs = [int(r["output_tokens"]) for r in mine]  # type: ignore[arg-type]
        print(
            f"{name:20} median out={statistics.median(outs):6.0f} max={max(outs):5} "
            f"cap hits={sum(bool(r['hit_cap']) for r in mine)} "
            f"repeat={statistics.mean(float(r['repeated_share']) for r in mine):.3f} "  # type: ignore[arg-type]
            f"no-tool={sum(r['tool'] is None for r in mine)}",
            flush=True,
        )


if __name__ == "__main__":
    main()
