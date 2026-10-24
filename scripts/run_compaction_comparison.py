"""Paired Gemini-compaction trials using the unchanged engine comparison runtime."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from loopforge.adapters.model_lease import ModelLease
from loopforge.adapters.paid_budget import SQLitePaidBudget
from scripts import run_engine_comparison as cmp
from scripts.compaction_lab import (
    COOLDOWN_CALLS,
    MODEL,
    PRICE_VALID_UNTIL,
    TRIGGER_TOKENS,
    CompactingModel,
    GeminiCompactor,
)

ROOT = Path(__file__).resolve().parents[1]
BUDGET = 2_000_000
SCHEDULE = [(1, "ollama", "cold"), (1, "mlx", "cold"), (2, "mlx", "warm"), (2, "ollama", "warm")]


def child(directory, engine, budget_path):
    ledger = SQLitePaidBudget(budget_path, BUDGET)
    compactor = GeminiCompactor(ledger, directory / "compactor")
    original = cmp.OllamaModel
    cmp.OllamaModel = lambda **kw: CompactingModel(
        compactor=compactor, trial_directory=directory, **kw
    )
    try:
        cmp.child(directory, engine)
    finally:
        cmp.OllamaModel = original
    path = directory / "result.json"
    result = json.loads(path.read_text())
    compactions = [json.loads(p.read_text()) for p in sorted(directory.glob("compaction-*.json"))]
    result["compaction"] = {
        "attempts": len(compactions),
        "applied": sum(c["applied"] for c in compactions),
        "wall_seconds": sum(c["wall_seconds"] for c in compactions),
        "events": compactions,
    }
    path.write_text(json.dumps(result, indent=2))


def frozen_paths():
    return [
        *sorted((ROOT / "src").rglob("*.py")),
        *sorted((ROOT / "scripts").glob("*.py")),
        cmp.ORACLE,
        ROOT / "scripts/ollama_renderer/chat_template.jinja",
        cmp.TRIAL / "repository-intake.json",
    ]


def run(output, baseline):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    hashes = {str(p.relative_to(ROOT)): cmp.sha(p) for p in frozen_paths()}
    ledger = SQLitePaidBudget(output / "paid-budget.sqlite", BUDGET)
    plan = {
        "kind": "compaction-experiment",
        "max_additional_microusd": BUDGET,
        "authorization": "User authorized $2 for compaction throughput/time experiments",
        "baseline": str(baseline.resolve()),
        "schedule": SCHEDULE,
        "pinned_hashes": hashes,
        "compactor_model": MODEL,
        "pricing_source": "https://ai.google.dev/gemini-api/docs/pricing",
        "input_microusd_per_token": 0.3,
        "output_microusd_per_token": 2.5,
        "price_valid_until": PRICE_VALID_UNTIL.isoformat(),
        "trigger_tokens": TRIGGER_TOKENS,
        "minimum_calls_between_attempts": COOLDOWN_CALLS,
        "maximum_trial_seconds": cmp.SECONDS,
        "profile": cmp.native_options(),
        "mlx_presence_context_size": cmp.MLX_PRESENCE_CONTEXT,
        "scope": "local coding completion with independent checks; no frontier review",
    }
    (output / "plan.json").write_text(json.dumps(plan, indent=2))
    print(json.dumps({"status": "waiting_for_baseline", "directory": str(output)}), flush=True)
    # Never compete for the GPU or change an in-flight baseline's inputs.
    baseline_plan = json.loads((baseline / "plan.json").read_text())
    while True:
        if any(cmp.sha(ROOT / name) != digest for name, digest in hashes.items()):
            raise RuntimeError("Frozen experiment inputs changed before launch")
        try:
            done = json.loads((baseline / "results.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            done = []
        if len(done) == len(baseline_plan["schedule"]):
            break
        if (baseline / "monitor-stop.json").exists():
            raise RuntimeError(
                "Baseline stopped for harness/configuration fault; review before compaction trials"
            )
        time.sleep(10)
    lease = ModelLease()
    lease.acquire("compaction-comparison")
    results = []
    try:
        for rep, engine, mode in SCHEDULE:
            if any(cmp.sha(ROOT / name) != digest for name, digest in hashes.items()):
                raise RuntimeError("Frozen experiment inputs changed")
            name = f"rep{rep}-{engine}-{mode}"
            directory = output / name
            provider = process = None
            started = time.monotonic()
            preparation = {}
            print(json.dumps({"event": "trial_start", "trial": name}), flush=True)
            try:
                cmp.unload_ollama()
                if engine == "mlx":
                    provider, ready = cmp.start_mlx()
                    preparation["server_ready_seconds"] = ready
                    if mode == "warm":
                        preparation |= cmp.warm_mlx()
                elif mode == "warm":
                    preparation |= cmp.warm_ollama()
                with (output / f"{name}.log").open("w") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--child",
                            str(directory),
                            engine,
                            str(output / "paid-budget.sqlite"),
                        ],
                        cwd=ROOT,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    try:
                        process.wait(timeout=cmp.SECONDS + 300)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
                path = directory / "result.json"
                result = (
                    json.loads(path.read_text())
                    if path.exists()
                    else {"status": "timeout_or_crash", "verified_success": False}
                )
            finally:
                if process and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                if provider:
                    cmp.mlx_lab.stop_server(provider)
                cmp.unload_ollama()
                cmp.cleanup_containers(directory / "workspace")
            row = {
                "trial": name,
                "engine": engine,
                "mode": mode,
                "rep": rep,
                **preparation,
                **result,
                "total_wall_seconds": time.monotonic() - started,
            }
            results.append(row)
            (output / "results.json").write_text(json.dumps(results, indent=2))
            print(
                json.dumps(
                    {
                        "event": "trial_end",
                        "trial": name,
                        "passed": row["verified_success"],
                        "seconds": row["total_wall_seconds"],
                        "paid_spent_microusd": ledger.account().spent,
                    }
                ),
                flush=True,
            )
    finally:
        lease.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--child", nargs=3)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    if args.child:
        child(Path(args.child[0]), args.child[1], Path(args.child[2]))
    elif args.output and args.baseline:
        run(args.output, args.baseline)
    else:
        parser.error("Provide --output and --baseline")


if __name__ == "__main__":
    main()
