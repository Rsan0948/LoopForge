# ruff: noqa: EM101, PLR0915, PT018, TRY003, TRY301 - operator experiment script
"""Ollama vs MLX on the real coding task: same runtime, same prompt bytes, cold and warm.

Each trial runs the frozen qwen-native-v1 / qwen3.6:35b-a3b worker unchanged (32768
output, 131072 context, vendor sampling, evidence-driven thinking) on the frozen
full-a3b reconciliation task, inside the normal container sandbox and fixed checks.
No supervisor, review or paid calls.

Prompt parity: MLX receives Ollama's own rendering of every request (Go oracle built
from Ollama's renderer; the MLX template echoes it) and parses tools with the matching
qwen3_coder parser. Each engine otherwise runs its shipped deployment: Ollama's model
defaults (including its draft setting) and the pinned MLX server; this compares
deployments, and the quantizations differ (GGUF Q4_K_M vs MLX affine 4-bit).

Cache control, identical for both engines:
  cold  - model unloaded / server restarted; the first trial request pays the load.
  warm  - fresh load, then one short unrelated warm-up request, so weights are
          resident and kernels compiled but no task prefix is cached.
Within a trial, prefix caching across turns is normal for both engines. OS file
cache of the weights is not purged (needs sudo); cold means process-cold.
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

import httpx

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.model_lease import ModelLease
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.domain.events import ActionProposed, RunStopped
from loopforge.domain.types import RunStatus
from loopforge.entrypoints import campaign_coding as coding
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from scripts import mlx_lab
from scripts.run_mlx_comparison import native_response

ROOT = Path(__file__).resolve().parents[1]
TRIAL = ROOT / ".loopforge/tasks/reconciliation-forced-attempt/full-a3b-2026-10-23"
ORACLE = ROOT / ".loopforge/mlx-lab/renderer-oracle/renderer-oracle"
RENDER_TEMPLATE = (ROOT / "scripts/ollama_renderer/chat_template.jinja").read_text()
OLLAMA = "http://127.0.0.1:11434"
MLX = "http://127.0.0.1:18181"
MODEL = "qwen3.6:35b-a3b"
SECONDS = 2400
# Upstream mlx_lm.server stores no reusable checkpoint for tool-final requests, which
# disables prefix reuse on this hybrid model in an agent loop; see mlx_server_agentic.py.
AGENTIC_CACHE = True
TURNS = 60
WARMUP = [{"role": "user", "content": "Reply with the single word OK."}]
NATIVE_SUFFIX = (
    "\nThis is one continuous conversation: your earlier tool calls and "
    "their results stay above. Call review_feedback once for earlier "
    "attempts' check evidence. Choose reads, edits and registered checks "
    "to complete the task, and run the failing check after a coherent "
    "change. Use current source hashes for edits. The supervisor "
    "assignment is guidance; original acceptance, allowed paths, protected "
    "files, commands and budgets remain mandatory. No merge or deployment."
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(native: dict[str, object]) -> str:
    out = subprocess.run(
        [str(ORACLE)],
        input=json.dumps(native),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)["prompt"]


def mlx_payload(native: dict[str, object], model_path: str) -> dict[str, object]:
    """Ollama-rendered prompt plus OpenAI-shaped framing; tool results pair positionally."""
    messages: list[dict[str, object]] = []
    last_ids: list[str] = []
    for i, original in enumerate(native["messages"]):  # type: ignore[arg-type]
        message: dict[str, object] = {
            "role": original["role"],
            "content": original.get("content", ""),
        }
        if original["role"] == "assistant" and original.get("tool_calls"):
            last_ids = []
            calls = []
            for j, call in enumerate(original["tool_calls"]):
                last_ids.append(f"call_{i}_{j}")
                calls.append(
                    {
                        "id": last_ids[-1],
                        "type": "function",
                        "function": {
                            "name": call["function"]["name"],
                            "arguments": json.dumps(call["function"].get("arguments", {})),
                        },
                    }
                )
            message["tool_calls"] = calls
        if original["role"] == "tool":
            message["name"] = original.get("tool_name", original.get("name"))
            message["tool_call_id"] = last_ids.pop(0) if last_ids else f"orphan_{i}"
        messages.append(message)
    opts = native["options"]  # type: ignore[index]
    return {
        "model": model_path,
        "messages": messages,
        "tools": native.get("tools", []),
        "stream": False,
        "max_tokens": opts["num_predict"],
        "temperature": opts["temperature"],
        "top_p": opts["top_p"],
        "top_k": opts["top_k"],
        "min_p": opts["min_p"],
        "presence_penalty": opts["presence_penalty"],
        "presence_context_size": 64,  # Ollama's repeat_last_n default window
        "repetition_penalty": opts.get("repeat_penalty", 1.0),
        "chat_template_kwargs": {"ollama_rendered_prompt": render(native)},
    }


class Transport(httpx.BaseTransport):
    def __init__(self, engine: str, directory: Path, deadline: float) -> None:
        self.engine, self.directory, self.deadline = engine, directory, deadline
        self.calls = 0
        self.client = httpx.Client(trust_env=False)
        self.model_path = mlx_lab.model_info()["local_path"] if engine == "mlx" else ""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        native = json.loads(request.content)
        if self.engine == "mlx":
            payload, url = mlx_payload(native, self.model_path), f"{MLX}/v1/chat/completions"
        else:
            payload, url = native, f"{OLLAMA}/api/chat"
        record: dict[str, object] = {
            "engine": self.engine,
            "call": self.calls,
            "think": native.get("think"),
            "native_request": native,
        }
        started = time.monotonic()
        try:
            remaining = self.deadline - started
            if remaining <= 0:
                raise TimeoutError("trial deadline reached")
            response = self.client.post(url, json=payload, timeout=min(1200, remaining))
            record |= {
                "http_status": response.status_code,
                "wall_seconds": time.monotonic() - started,
            }
            if not response.is_success:
                record["error_body"] = response.text[:2000]
                return httpx.Response(response.status_code, content=response.content)
            data = response.json()
            record["response"] = data
            normalized = native_response(data) if self.engine == "mlx" else data
            calls = normalized.get("message", {}).get("tool_calls", [])
            print(
                json.dumps(
                    {
                        "event": "model_call",
                        "call": self.calls,
                        "think": native.get("think"),
                        "seconds": round(record["wall_seconds"], 1),  # type: ignore[arg-type]
                        "prompt_tokens": normalized.get("prompt_eval_count"),
                        "output_tokens": normalized.get("eval_count"),
                        "tools": [c["function"]["name"] for c in calls],
                    }
                ),
                flush=True,
            )
            return httpx.Response(200, json=normalized)
        except Exception as exc:
            record |= {"error": str(exc), "wall_seconds": time.monotonic() - started}
            raise
        finally:
            (self.directory / f"exchange-{self.calls:03}.json").write_text(json.dumps(record))

    def close(self) -> None:
        self.client.close()


def task_inputs():
    config = RepositoryGoalConfig.model_validate_json(
        (TRIAL / "repository-intake.json").read_text()
    )
    campaign = config.campaign.model_copy(
        update={
            "max_iterations": TURNS,
            "max_seconds": SECONDS,
            "max_tokens": 2_000_000,
            "model_timeout_seconds": 1200,
            "repair_escalation": False,
            "repair_decisions": False,
            "force_repair_attempt": False,
        }
    )
    assert campaign.model == MODEL and campaign.inference_profile == "qwen-native-v1"
    assert config.task.sandbox_max_open_files == 1024
    return config, campaign


def child(directory: Path, engine: str) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    config, campaign = task_inputs()
    task = config.task
    (directory / "task.json").write_text(task.model_dump_json(indent=2))
    workspace = coding.prepare_checkout(
        Path(campaign.repository), campaign.revision, directory / "workspace", task.branch
    )
    store = SQLiteEventStore(directory / "events.sqlite", codec=JsonEventCodec())
    started = time.monotonic()
    transport = Transport(engine, directory, started + SECONDS)
    original = coding.OllamaModel
    coding.OllamaModel = lambda **kw: OllamaModel(**kw, transport=transport)  # type: ignore[assignment]
    result: dict[str, object] = {"engine": engine}
    runtime = run_id = live = None
    try:
        runtime, live = coding.build_coding(campaign, task, workspace, store=store)
        coding.OllamaModel = original
        run_id = runtime.start(task.objective + NATIVE_SUFFIX)
        state = runtime.state_for(run_id)
        while not state.status.is_terminal and time.monotonic() < started + SECONDS:
            state = runtime.step(run_id)
        if not state.status.is_terminal:
            state = runtime.cancel(run_id, summary="comparison deadline")
        events = store.events_for(run_id)
        final = runtime.verifier.verify(state)
        result |= {
            "status": state.status.value,
            "verified_success": state.status == RunStatus.SUCCEEDED and final.passed,
            "final_summary": final.summary[:3000],
            "turns": state.iteration,
            "tokens_in": state.input_tokens,
            "tokens_out": state.output_tokens,
            "model_calls": transport.calls,
            "actions": [
                {"tool": e.proposal.tool_name, "arguments": dict(e.proposal.arguments)}
                for e in events
                if isinstance(e, ActionProposed)
            ],
            "stop": [e.summary for e in events if isinstance(e, RunStopped)],
        }
        (directory / "candidate.patch").write_text(workspace.diff())
    except BaseException as exc:
        result |= {
            "status": "error",
            "verified_success": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        if runtime is not None and run_id is not None:
            runtime.cancel(run_id, summary="comparison worker interrupted")
    finally:
        coding.OllamaModel = original
        if live is not None:
            live.close()
        transport.close()
        result["wall_seconds"] = time.monotonic() - started
        (directory / "result.json").write_text(json.dumps(result, indent=2))
        print(
            json.dumps(
                {
                    k: v
                    for k, v in result.items()
                    if k not in {"actions", "final_summary", "traceback"}
                }
            ),
            flush=True,
        )


def unload_ollama() -> None:
    with httpx.Client(timeout=60, trust_env=False) as client:
        for item in client.get(f"{OLLAMA}/api/ps").raise_for_status().json()["models"]:
            client.post(
                f"{OLLAMA}/api/generate", json={"model": item["name"], "keep_alive": 0}
            ).raise_for_status()
        deadline = time.monotonic() + 60
        while client.get(f"{OLLAMA}/api/ps").json()["models"] and time.monotonic() < deadline:
            time.sleep(1)


def native_options() -> dict[str, object]:
    profile = coding.profile_for("coding", native=True, model=MODEL)
    return profile.options()


def warm_ollama() -> dict[str, object]:
    """Load with the trial's exact options (a different num_ctx would reload), unrelated prompt."""
    started = time.monotonic()
    with httpx.Client(timeout=900, trust_env=False) as client:
        data = (
            client.post(
                f"{OLLAMA}/api/chat",
                json={
                    "model": MODEL,
                    "stream": False,
                    "think": False,
                    "keep_alive": "30m",
                    "messages": WARMUP,
                    "options": {**native_options(), "num_predict": 8},
                },
            )
            .raise_for_status()
            .json()
        )
    return {
        "warmup_seconds": time.monotonic() - started,
        "load_seconds": data.get("load_duration", 0) / 1e9,
    }


def start_mlx() -> tuple[subprocess.Popen[bytes], float]:
    started = time.monotonic()
    provider = mlx_lab.server(True, 18181, RENDER_TEMPLATE, agentic_cache=AGENTIC_CACHE)
    deadline = started + 300
    while time.monotonic() < deadline:
        if provider.poll() is not None:
            raise RuntimeError("MLX server exited")
        try:
            if httpx.get(f"{MLX}/v1/models", timeout=2, trust_env=False).is_success:
                return provider, time.monotonic() - started
        except httpx.HTTPError:
            pass
        time.sleep(1)
    mlx_lab.stop_server(provider)
    raise TimeoutError("MLX startup timeout")


def warm_mlx() -> dict[str, object]:
    started = time.monotonic()
    native = {
        "model": MODEL,
        "messages": WARMUP,
        "tools": [],
        "think": False,
        "options": {**native_options(), "num_predict": 8},
    }
    payload = mlx_payload(native, mlx_lab.model_info()["local_path"])
    with httpx.Client(timeout=900, trust_env=False) as client:
        client.post(f"{MLX}/v1/chat/completions", json=payload).raise_for_status()
    return {"warmup_seconds": time.monotonic() - started}


def cleanup_containers(workspace: Path) -> None:
    ids = subprocess.run(
        ["docker", "ps", "-q"], capture_output=True, text=True, check=True, timeout=15
    ).stdout.split()
    for cid in ids:
        info = json.loads(
            subprocess.run(
                ["docker", "inspect", cid], capture_output=True, text=True, check=True, timeout=15
            ).stdout
        )[0]
        if any(m.get("Source") == str(workspace) for m in info.get("Mounts", [])):
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=15, check=True)


SCHEDULE = [  # alternate engine and mode order between repetitions
    (1, "ollama", "cold"),
    (1, "mlx", "cold"),
    (1, "mlx", "warm"),
    (1, "ollama", "warm"),
    (2, "mlx", "cold"),
    (2, "ollama", "cold"),
    (2, "ollama", "warm"),
    (2, "mlx", "warm"),
]


def run(output: Path, only: str | None) -> None:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    pinned = [
        *sorted((ROOT / "src").rglob("*.py")),
        Path(__file__).resolve(),
        ROOT / "scripts/mlx_lab.py",
        ROOT / "scripts/run_mlx_comparison.py",
        ROOT / "scripts/mlx_server_agentic.py",
        ROOT / "scripts/ollama_renderer/chat_template.jinja",
        ORACLE,
    ]
    hashes = {str(p.relative_to(ROOT)): sha(p) for p in pinned}
    commit = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    with httpx.Client(timeout=10, trust_env=False) as client:
        ollama_identity = {
            "version": client.get(f"{OLLAMA}/api/version").raise_for_status().json(),
            "show": client.post(f"{OLLAMA}/api/show", json={"model": MODEL})
            .raise_for_status()
            .json()
            .get("details"),
        }
    schedule = [s for s in SCHEDULE if only is None or s[1] == only]
    plan = {
        "commit": commit,
        "pinned_hashes": hashes,
        "ollama": ollama_identity,
        "mlx_model": mlx_lab.model_info(),
        "task_intake_sha256": sha(TRIAL / "repository-intake.json"),
        "seconds": SECONDS,
        "turns": TURNS,
        "schedule": schedule,
        "profile": native_options(),
        "mlx_server": "agentic-tool-checkpoint" if AGENTIC_CACHE else "upstream",
        "cache_modes": __doc__.split("Cache control")[1],
    }
    (output / "plan.json").write_text(json.dumps(plan, indent=2))
    lease = ModelLease()
    lease.acquire("engine-comparison")
    results: list[dict[str, object]] = []
    try:
        for rep, engine, mode in schedule:
            if any(sha(ROOT / name) != digest for name, digest in hashes.items()):
                raise ValueError("pinned runtime changed; stopping before another trial")
            name = f"rep{rep}-{engine}-{mode}"
            print(json.dumps({"event": "trial_start", "trial": name}), flush=True)
            provider = proc = None
            prep: dict[str, object] = {}
            started = time.monotonic()
            try:
                unload_ollama()
                if engine == "mlx":
                    provider, ready = start_mlx()
                    prep["server_ready_seconds"] = ready
                    if mode == "warm":
                        prep |= warm_mlx()
                elif mode == "warm":
                    prep |= warm_ollama()
                with (output / f"{name}.log").open("w") as log:
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--child",
                            str(output / name),
                            engine,
                        ],
                        cwd=ROOT,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    try:
                        proc.wait(timeout=SECONDS + 300)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait(timeout=5)
                path = output / name / "result.json"
                result = (
                    json.loads(path.read_text())
                    if path.exists()
                    else {"verified_success": False, "status": "timeout_or_crash"}
                )
            except Exception as exc:
                result = {
                    "verified_success": False,
                    "status": "infrastructure_error",
                    "error": str(exc),
                }
            finally:
                if proc and proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                if provider:
                    mlx_lab.stop_server(provider)
                unload_ollama()
                cleanup_containers(output / name / "workspace")
            row = {
                "trial": name,
                "rep": rep,
                "engine": engine,
                "mode": mode,
                **prep,
                **{k: v for k, v in result.items() if k not in {"actions", "traceback"}},
                "total_wall_seconds": time.monotonic() - started,
            }
            results.append(row)
            (output / "results.json").write_text(json.dumps(results, indent=2))
            print(
                json.dumps(
                    {
                        "event": "trial_end",
                        "trial": name,
                        "status": row.get("status"),
                        "passed": row.get("verified_success"),
                        "seconds": round(row["total_wall_seconds"], 1),
                    }
                ),
                flush=True,
            )
    finally:
        lease.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only", choices=["ollama", "mlx"])
    parser.add_argument("--child", nargs=2)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    if args.child:
        child(Path(args.child[0]), args.child[1])
    elif args.output:
        run(args.output, args.only)
    else:
        parser.error("--output is required")


if __name__ == "__main__":
    main()
