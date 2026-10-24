# ruff: noqa: EM101, TRY003, E501 - operator tuning script
"""Tuning: prefill/decode cost vs prompt depth, per engine setting. Not a task benchmark.

Replays requests captured from a real trial at several depths, each with a fixed 256-token
output, and reports prefill and decode per token. Ollama settings run on a private
`ollama serve` (separate port and environment) so the user's service is never reconfigured.

Ollama configurations: num_ctx {32768, 131072} x KV cache {q8_0, f16}. MLX: the pinned
server at the same depths. Draft acceptance comes from the private server's own log.

Usage: .venv/bin/python scripts/bench_engine_depth.py TRIAL_DIR OUTPUT_DIR [--mlx-only|--ollama-only]
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

from loopforge.adapters.model_lease import ModelLease
from scripts import mlx_lab
from scripts import run_engine_comparison as cmp

PORT = 11500
URL = f"http://127.0.0.1:{PORT}"
CALLS = (6, 12, 17, 19)  # about 8.8K, 17.4K, 25.1K and 29.8K prompt tokens in the evidence trial
OUTPUT = 256
CONFIGS = [(32768, "q8_0"), (131072, "q8_0"), (32768, "f16"), (131072, "f16")]
ACCEPT = re.compile(r"draft acceptance = ([0-9.]+)")


def requests(trial: Path) -> list[dict[str, object]]:
    out = []
    for call in CALLS:
        record = json.loads((trial / f"exchange-{call:03}.json").read_text())
        native = record["native_request"]
        native["stream"] = False
        out.append(native)
    return out


def serve(kv: str, log: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy() | {
        "OLLAMA_HOST": f"127.0.0.1:{PORT}",
        "OLLAMA_FLASH_ATTENTION": "1",
        "OLLAMA_KV_CACHE_TYPE": kv,
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_KEEP_ALIVE": "30m",
    }
    process = subprocess.Popen(
        ["/opt/homebrew/bin/ollama", "serve"],
        env=env,
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{URL}/api/version", timeout=2, trust_env=False).is_success:
                return process
        except httpx.HTTPError:
            time.sleep(0.5)
    process.terminate()
    raise TimeoutError("private ollama serve did not start")


def ollama_point(native: dict[str, object], num_ctx: int, log: Path) -> dict[str, object]:
    options = {**native["options"], "num_ctx": num_ctx, "num_predict": OUTPUT, "seed": 7}  # type: ignore[dict-item]
    request = {**native, "options": options}
    rows = []
    for _ in range(2):  # first pays the full prefill; the second reuses the cached prefix
        before = log.read_text(errors="replace")
        data = (
            httpx.post(f"{URL}/api/chat", json=request, timeout=1800, trust_env=False)
            .raise_for_status()
            .json()
        )
        accepted = ACCEPT.findall(log.read_text(errors="replace")[len(before) :])
        rows.append(
            {
                "prompt_tokens": data["prompt_eval_count"],
                "prefill_ms_per_token": 1000
                * data["prompt_eval_duration"]
                / 1e9
                / max(1, data["prompt_eval_count"]),
                "prefilled_tokens": data["prompt_eval_count"],
                "output_tokens": data["eval_count"],
                "decode_ms_per_token": 1000
                * data["eval_duration"]
                / 1e9
                / max(1, data["eval_count"]),
                "decode_tok_s": data["eval_count"] / (data["eval_duration"] / 1e9),
                "draft_acceptance": float(accepted[-1]) if accepted else None,
            }
        )
    return {"cold_prefix": rows[0], "cached_prefix": rows[1]}


def mlx_point(native: dict[str, object], model: str) -> dict[str, object]:
    def post(max_tokens: int) -> tuple[float, dict[str, object]]:
        payload = cmp.mlx_payload(native, model)
        payload |= {"max_tokens": max_tokens, "seed": 7}
        started = time.monotonic()
        data = (
            httpx.post(
                f"{cmp.MLX}/v1/chat/completions", json=payload, timeout=1800, trust_env=False
            )
            .raise_for_status()
            .json()
        )
        return time.monotonic() - started, data

    prefill_wall, first = post(1)
    overhead, _ = post(1)
    decode_wall, last = post(OUTPUT + 1)
    usage = first["usage"]
    produced = last["usage"]["completion_tokens"]
    return {
        "prompt_tokens": usage["prompt_tokens"],
        "prefill_ms_per_token": 1000 * prefill_wall / usage["prompt_tokens"],
        "cached_tokens_on_repeat": (last["usage"].get("prompt_tokens_details") or {}).get(
            "cached_tokens"
        ),
        "output_tokens": produced,
        "decode_ms_per_token": 1000 * (decode_wall - overhead) / max(1, produced - 1),
        "decode_tok_s": max(1, produced - 1) / max(1e-6, decode_wall - overhead),
    }


def main() -> None:
    trial, output = Path(sys.argv[1]), Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    natives = requests(trial)
    results: dict[str, object] = {"calls": CALLS, "output_tokens": OUTPUT}
    lease = ModelLease()
    lease.acquire("engine-depth-tuning")
    try:
        cmp.unload_ollama()
        if "--mlx-only" not in sys.argv:
            for num_ctx, kv in CONFIGS:
                label = f"ollama ctx={num_ctx} kv={kv}"
                log = output / f"ollama-{num_ctx}-{kv}.log"
                process = serve(kv, log)
                try:
                    points = []
                    for native in natives:
                        point = ollama_point(native, num_ctx, log)
                        points.append(point)
                        c = point["cached_prefix"]
                        print(
                            f"{label:28} depth={c['prompt_tokens']:6} "
                            f"prefill={point['cold_prefix']['prefill_ms_per_token']:.2f}ms/t "
                            f"decode={c['decode_ms_per_token']:.1f}ms/t "
                            f"({c['decode_tok_s']:.1f} tok/s) accept={c['draft_acceptance']}",
                            flush=True,
                        )
                    results[label] = points
                finally:
                    process.terminate()
                    process.wait(timeout=30)
                (output / "depth.json").write_text(json.dumps(results, indent=2))
        if "--ollama-only" not in sys.argv:
            provider, _ = cmp.start_mlx()
            try:
                cmp.warm_mlx()
                model = mlx_lab.model_info()["local_path"]
                points = []
                for native in natives:
                    point = mlx_point(native, model)
                    points.append(point)
                    print(
                        f"{'mlx':28} depth={point['prompt_tokens']:6} "
                        f"prefill={point['prefill_ms_per_token']:.2f}ms/t "
                        f"decode={point['decode_ms_per_token']:.1f}ms/t "
                        f"({point['decode_tok_s']:.1f} tok/s) cached={point['cached_tokens_on_repeat']}",
                        flush=True,
                    )
                results["mlx"] = points
            finally:
                mlx_lab.stop_server(provider)
            (output / "depth.json").write_text(json.dumps(results, indent=2))
    finally:
        lease.release()
    print("saved", output / "depth.json")


if __name__ == "__main__":
    main()
