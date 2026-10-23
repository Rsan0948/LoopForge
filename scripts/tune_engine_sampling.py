"""Tuning: sample one fixed captured request on each engine/setting; compare output length.

Same native request and prompt bytes everywhere (MLX gets Ollama's rendering). Reports
output tokens, wall time, decode rate and the chosen tool per sample. No task execution.

Usage: .venv/bin/python scripts/tune_engine_sampling.py EXCHANGE_JSON SAMPLES [CTX ...]
CTX values are MLX presence_context_size settings to try (default: 64 256 2048).
"""

import json
import statistics
import sys
import time
from pathlib import Path

import httpx

from loopforge.adapters.model_lease import ModelLease
from scripts import mlx_lab
from scripts import run_engine_comparison as cmp
from scripts.run_mlx_comparison import native_response


def summarize(label: str, rows: list[dict[str, object]]) -> None:
    outs = [int(r["out"]) for r in rows]  # type: ignore[arg-type]
    print(
        f"{label:22} out tokens median={statistics.median(outs):6.0f} "
        f"min={min(outs):5} max={max(outs):5}  wall median="
        f"{statistics.median(float(r['wall']) for r in rows):5.1f}s  "  # type: ignore[arg-type]
        f"tools={[r['tool'] for r in rows]}",
        flush=True,
    )


def main() -> None:
    exchange, samples = Path(sys.argv[1]), int(sys.argv[2])
    contexts = [int(c) for c in sys.argv[3:] if c.isdigit()] or [64, 256, 2048]
    native = json.loads(exchange.read_text())["native_request"]
    native["think"] = True
    lease = ModelLease()
    lease.acquire("engine-sampling-tuning")
    results: dict[str, list[dict[str, object]]] = {}
    try:
        cmp.unload_ollama()
        cmp.warm_ollama()
        for ctx in contexts:
            rows = []
            request = {**native, "options": {**native["options"], "repeat_last_n": ctx}}
            for _ in range(samples):
                t = time.monotonic()
                data = (
                    httpx.post(
                        f"{cmp.OLLAMA}/api/chat", json=request, timeout=1200, trust_env=False
                    )
                    .raise_for_status()
                    .json()
                )
                calls = data["message"].get("tool_calls") or []
                rows.append(
                    {
                        "out": data["eval_count"],
                        "wall": time.monotonic() - t,
                        "tool": calls[0]["function"]["name"] if calls else None,
                    }
                )
            results[f"ollama repeat_last_n={ctx}"] = rows
            summarize(f"ollama repeat_last_n={ctx}", rows)
        cmp.unload_ollama()
        if "--ollama-only" in sys.argv:
            return
        provider, _ = cmp.start_mlx()
        try:
            cmp.warm_mlx()
            model = mlx_lab.model_info()["local_path"]
            for ctx in contexts:
                rows = []
                for _ in range(samples):
                    payload = cmp.mlx_payload(native, model)
                    payload["presence_context_size"] = ctx
                    t = time.monotonic()
                    data = (
                        httpx.post(
                            f"{cmp.MLX}/v1/chat/completions",
                            json=payload,
                            timeout=1200,
                            trust_env=False,
                        )
                        .raise_for_status()
                        .json()
                    )
                    n = native_response(data)
                    calls = n["message"]["tool_calls"]
                    rows.append(
                        {
                            "out": n["eval_count"],
                            "wall": time.monotonic() - t,
                            "tool": calls[0]["function"]["name"] if calls else None,
                        }
                    )
                results[f"mlx ctx={ctx}"] = rows
                summarize(f"mlx presence_ctx={ctx}", rows)
        finally:
            mlx_lab.stop_server(provider)
    finally:
        cmp.unload_ollama()
        lease.release()
    out = exchange.parent / "sampling-tuning.json"
    out.write_text(json.dumps(results, indent=2))
    print("saved", out)


if __name__ == "__main__":
    main()
