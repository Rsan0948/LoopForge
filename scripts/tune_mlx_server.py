"""Tuning: MLX server flags on one long captured request. Prefill (max_tokens=1, cold
cache) and decode (cache hit) per configuration. No task execution.

Usage: .venv/bin/python -m scripts.tune_mlx_server EXCHANGE_JSON
"""

import json
import sys
import time
from pathlib import Path

import httpx

from loopforge.adapters.model_lease import ModelLease
from scripts import mlx_lab
from scripts import run_engine_comparison as cmp
from scripts.run_mlx_comparison import native_response

CONFIGS: dict[str, tuple[str, ...]] = {
    "default": (),
    "kv8": ("--kv-bits", "8"),
    "prefill4096": ("--prefill-step-size", "4096"),
    "kv8+prefill4096": ("--kv-bits", "8", "--prefill-step-size", "4096"),
}


def timed(payload: dict[str, object]) -> tuple[float, dict[str, object]]:
    started = time.monotonic()
    data = (
        httpx.post(f"{cmp.MLX}/v1/chat/completions", json=payload, timeout=1200, trust_env=False)
        .raise_for_status()
        .json()
    )
    return time.monotonic() - started, native_response(data)


def main() -> None:
    native = json.loads(Path(sys.argv[1]).read_text())["native_request"]
    native["think"] = True
    lease = ModelLease()
    lease.acquire("mlx-server-tuning")
    results = {}
    try:
        cmp.unload_ollama()
        for name, args in CONFIGS.items():
            provider, _ = cmp.start_mlx(args)
            try:
                cmp.warm_mlx()
                model = mlx_lab.model_info()["local_path"]
                cold = cmp.mlx_payload(native, model)
                cold["max_tokens"] = 1
                prefill_s, first = timed(cold)
                hot = cmp.mlx_payload(native, model)
                hot["max_tokens"] = 600
                hot["presence_context_size"] = 2048
                wall, second = timed(hot)
                decode = second["eval_count"] / wall if wall else 0
                results[name] = {
                    "prompt_tokens": first["prompt_eval_count"],
                    "prefill_seconds": prefill_s,
                    "prefill_tok_s": first["prompt_eval_count"] / prefill_s,
                    "decode_tokens": second["eval_count"],
                    "cached_on_second": second["prompt_eval_cached_count"],
                    "decode_tok_s_upper": decode,
                }
                print(
                    name,
                    json.dumps(
                        {
                            k: round(v, 1) if isinstance(v, float) else v
                            for k, v in results[name].items()
                        }
                    ),
                    flush=True,
                )
            finally:
                mlx_lab.stop_server(provider)
    finally:
        lease.release()
    (Path(sys.argv[1]).parent / "mlx-server-tuning.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
