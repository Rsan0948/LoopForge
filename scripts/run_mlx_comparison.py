"""Bounded local engine comparison, reusing the real coding runtime and checks.

No supervisor, review model, escalation or paid calls. The only provider change
is a transport bridge: existing OllamaModel owns prompt/history/schema handling.
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
from dataclasses import replace
from pathlib import Path

import httpx

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.model_lease import ModelLease
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.domain.events import ActionProposed, RunStopped
from loopforge.domain.types import RunStatus
from loopforge.entrypoints import campaign_coding as coding
from loopforge.entrypoints.campaign_models import AcceptanceProbe, CodingTaskConfig
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from scripts import mlx_lab

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / ".loopforge/mlx-lab"
SECONDS = 600
TURNS = 24
OUTPUT = 4096

# Trusted additional quality check for the passing-baseline coverage task.
# Only the test file is editable, so branch locations in server/parser are fixed.
COVERAGE_PROBE = """import sys, threading, pathlib, pytest
root=pathlib.Path('/workspace')
targets={}
for name,needle in [
 ('app/server.py','raise HTTPException(409, "conflicting document'),
 ('app/pdf_documents.py','raise PdfError(422, "JOB_ID does not match job")')
]:
 lines=(root/name).read_text().splitlines()
 matches=[i for i,s in enumerate(lines,1) if needle in s]
 assert len(matches)==1, (name,matches)
 targets[(str(root/name),matches[0])]=False
asserts=(root/'app/tests/test_api.py').read_text().count('assert ')
assert asserts>=20, 'Do not remove existing API assertions'
def trace(frame,event,arg):
 key=(frame.f_code.co_filename,frame.f_lineno)
 if event=='line' and key in targets:targets[key]=True
 return trace
sys.settrace(trace);threading.settrace(trace)
try:code=pytest.main(['-q','-p','no:cacheprovider','-o','cache_dir=/tmp/mlx-comparison-pytest','app/tests/test_api.py'])
finally:sys.settrace(None);threading.settrace(None)
assert code==0,code
assert all(targets.values()), 'API tests must execute 422 and 409 branches: '+str(targets)
print('API regression coverage branches passed')
"""


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def mlx_request(native: dict[str, object], model: str) -> dict[str, object]:
    """Convert native chat framing only; tool argument values are never coerced."""
    messages, pending = [], []
    for i, original in enumerate(native["messages"]):
        message = {"role": original["role"], "content": original.get("content", "")}
        if original["role"] == "assistant" and original.get("tool_calls"):
            calls = []
            for j, call in enumerate(original["tool_calls"]):
                function = call["function"]
                call_id = f"call_{i}_{j}"
                pending.append((function["name"], call_id))
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": function["name"],
                            "arguments": json.dumps(function.get("arguments", {})),
                        },
                    }
                )
            message["tool_calls"] = calls
        if original["role"] == "tool":
            name = original.get("tool_name", original.get("name"))
            candidates = [p for p in pending if p[0] == name]
            if len(candidates) != 1:
                msg = "Ambiguous or orphan native tool result"
                raise ValueError(msg)
            message |= {"name": name, "tool_call_id": candidates[0][1]}
            pending.remove(candidates[0])
        if original.get("thinking"):
            message["reasoning_content"] = original["thinking"]
        messages.append(message)
    opts = native["options"]
    return {
        "model": model,
        "messages": messages,
        "tools": native.get("tools", []),
        "stream": False,
        "max_tokens": opts["num_predict"],
        "temperature": opts["temperature"],
        "top_p": opts.get("top_p", 0.95),
        "top_k": opts.get("top_k", 20),
        "min_p": opts.get("min_p", 0.0),
        "presence_penalty": opts.get("presence_penalty", 0.0),
        "presence_context_size": 64,
        "repetition_penalty": 1.0,
        "chat_template_kwargs": {"enable_thinking": native.get("think", False)},
    }


def native_response(data: dict[str, object]) -> dict[str, object]:
    """Translate to the existing strict native response schema; no invented timings."""
    message = data["choices"][0]["message"]
    calls = []
    for call in message.get("tool_calls", []):
        function = call["function"]
        args = function.get("arguments", "{}")
        calls.append(
            {
                "function": {
                    "name": function["name"],
                    "arguments": json.loads(args) if isinstance(args, str) else args,
                }
            }
        )
    return {
        "model": data["model"],
        "done": True,
        "done_reason": "length" if data["choices"][0]["finish_reason"] == "length" else "stop",
        "message": {
            "role": "assistant",
            "content": message.get("content") or "",
            "thinking": message.get("reasoning_content") or message.get("reasoning") or "",
            "tool_calls": calls,
        },
        "prompt_eval_count": data["usage"]["prompt_tokens"],
        "eval_count": data["usage"]["completion_tokens"],
        "prompt_eval_cached_count": data["usage"]
        .get("prompt_tokens_details", {})
        .get("cached_tokens", 0),
    }


class EngineTransport(httpx.BaseTransport):
    def __init__(self, engine, directory, deadline):
        self.engine, self.directory, self.deadline = engine, directory, deadline
        self.calls = 0
        self.client = httpx.Client(trust_env=False)

    def handle_request(self, request):
        self.calls += 1
        native = json.loads(request.content)
        if native["options"]["num_predict"] > OUTPUT:
            msg = "Frozen output limit exceeded"
            raise ValueError(msg)
        # Same penalty window/seed for both backends. No speculative decoding.
        native["options"] |= {"repeat_last_n": 64, "seed": 42, "draft_num_predict": 0}
        if self.engine == "mlx":
            payload = mlx_request(native, mlx_lab.model_info()["local_path"])
            payload["seed"] = 42
            url = "http://127.0.0.1:18181/v1/chat/completions"
        else:
            payload, url = native, "http://127.0.0.1:11434/api/chat"
        record = {
            "engine": self.engine,
            "call": self.calls,
            "native_request": native,
            "wire_request": payload,
        }
        dest = self.directory / f"exchange-{self.calls:03}.json"
        dest.write_text(json.dumps(record))
        started = time.monotonic()
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                msg = "Frozen trial deadline reached"
                raise TimeoutError(msg)  # noqa: TRY301 - explicit hard deadline
            response = self.client.post(url, json=payload, timeout=min(240, remaining))
            record |= {
                "http_status": response.status_code,
                "wall_seconds": time.monotonic() - started,
            }
            if response.is_success:
                data = response.json()
                record["response"] = data
                normalized = native_response(data) if self.engine == "mlx" else data
                calls = normalized.get("message", {}).get("tool_calls", [])
                print(
                    json.dumps(
                        {
                            "event": "model_call",
                            "call": self.calls,
                            "seconds": round(record["wall_seconds"], 2),
                            "tools": [c["function"]["name"] for c in calls],
                        }
                    ),
                    flush=True,
                )
                return httpx.Response(200, json=normalized)
            return httpx.Response(response.status_code, content=response.content)
        except Exception as exc:
            record |= {"error": str(exc), "wall_seconds": time.monotonic() - started}
            raise
        finally:
            dest.write_text(json.dumps(record))

    def close(self):
        self.client.close()


def make_task(entry, index):
    config = RepositoryGoalConfig.model_validate_json(Path(entry["intake"]).read_text())
    raw = config.task.model_dump(mode="json")
    raw |= {
        "sandbox_max_open_files": 1024,
        "branch": f"lab/engine-comparison-{index}",
        "required_changed_paths": [],
        "max_review_calls_after_coverage": 6,
    }
    if index == 2:
        raw |= {
            "kind": "feature",
            "allowed_paths": ["app/tests/test_api.py"],
            "editable_project_paths": ["app/tests/test_api.py"],
            "required_changed_paths": ["app/tests/test_api.py"],
            "objective": "Add missing API regression coverage in app/tests/test_api.py. "
            "Preserve idempotent same-PDF 200 coverage, restore wrong-job 422 coverage, "
            "and test a distinct valid PDF for the same job/kind returns 409. "
            "The existing app works. Only the API test file may change. "
            "Run check_project first to establish baseline; then inspect relevant source, "
            "make the test repair and run the fixed checks. The trusted coverage probe "
            "must observe both wrong-job and conflict branches during API tests. "
            "Do not weaken existing tests. CONTRACT.md is the specification.",
        }
        raw["acceptance_probes"].append(
            AcceptanceProbe(
                criterion_id="coverage",
                name="check_acceptance_coverage",
                description="API tests execute wrong-job 422 and distinct-document 409 branches",
                source=COVERAGE_PROBE,
                timeout_seconds=60,
            ).model_dump(mode="json")
        )
    else:
        raw["objective"] = (
            "Repair the preserved reconciliation app until all fixed checks and independent "
            "API, browser and launcher acceptance probes pass. Run check_project first, then "
            "investigate actual failures. CONTRACT.md decides behavior. Existing tests can "
            "contain incorrect expectations; correct those without removing coverage. "
            "Inspect app/server.py upload idempotency, app/tests/test_api.py, "
            "app/tests/test_calculations.py and app/static/styles.css as the likely relevant "
            "areas. Do not rebuild the app. Only app/ is editable. Keep changes compact."
        )
    return config, CodingTaskConfig.model_validate_json(json.dumps(raw))


def child(plan_path, job_index):
    plan = json.loads(Path(plan_path).read_text())
    job = plan["jobs"][job_index]
    out = Path(job["directory"])
    out.mkdir(parents=True, exist_ok=False)
    entry = plan["tasks"][job["task"]]
    config, generated = make_task(entry, job["task"])
    task = CodingTaskConfig.model_validate_json(json.dumps(plan["task_contracts"][job["task"]]))
    if task != generated:
        msg = "Frozen comparison task differs from generated task"
        raise ValueError(msg)
    (out / "task.json").write_text(task.model_dump_json(indent=2))
    campaign = config.campaign.model_copy(
        update={
            "model": "qwen3.6:35b-a3b",
            "inference_profile": "qwen-native-v1",
            "max_iterations": TURNS,
            "max_seconds": SECONDS,
            "max_tokens": 1000000,
            "model_timeout_seconds": 240,
            "repair_escalation": False,
            "repair_decisions": False,
            "force_repair_attempt": False,
        }
    )
    workspace = coding.prepare_checkout(
        Path(entry["source"]), entry["revision"], out / "workspace", task.branch
    )
    store = SQLiteEventStore(out / "events.sqlite", codec=JsonEventCodec())
    started = time.monotonic()
    transport = EngineTransport(job["engine"], out, started + SECONDS)
    original_model, original_profile = coding.OllamaModel, coding.profile_for

    def model_factory(**kwargs):
        return OllamaModel(**kwargs, transport=transport)

    def profile_factory(*args, **kwargs):
        return replace(original_profile(*args, **kwargs), output_tokens=OUTPUT)

    coding.OllamaModel, coding.profile_for = model_factory, profile_factory
    live = None
    runtime = run_id = None
    result = {"engine": job["engine"], "task": entry["name"], "repetition": job["repetition"]}
    try:
        runtime, live = coding.build_coding(
            campaign, task, workspace, store=store, require_revision=True
        )
        coding.OllamaModel, coding.profile_for = original_model, original_profile
        run_id = runtime.start(task.objective)
        (out / "run-id.txt").write_text(str(run_id))
        state = runtime.resume(run_id)
        events = store.events_for(run_id)
        final = runtime.verifier.verify(state)
        result |= {
            "status": state.status.value,
            "verified_success": state.status == RunStatus.SUCCEEDED and final.passed,
            "final_checks_passed": final.passed,
            "final_summary": final.summary,
            "iterations": state.iteration,
            "tokens_in": state.input_tokens,
            "tokens_out": state.output_tokens,
            "calls": transport.calls,
            "actions": [
                {"tool": e.proposal.tool_name, "arguments": dict(e.proposal.arguments)}
                for e in events
                if isinstance(e, ActionProposed)
            ],
            "stop": [e.summary for e in events if isinstance(e, RunStopped)],
        }
        (out / "candidate.patch").write_text(workspace.diff())
    except BaseException as exc:
        result |= {
            "status": "error",
            "verified_success": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        if runtime is not None and run_id is not None:
            runtime.cancel(run_id, summary="Bounded comparison worker interrupted")
    finally:
        coding.OllamaModel, coding.profile_for = original_model, original_profile
        if live is not None:
            live.close()
        result["wall_seconds"] = time.monotonic() - started
        (out / "result.json").write_text(json.dumps(result, indent=2))
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


def unload_ollama():
    with httpx.Client(timeout=30, trust_env=False) as client:
        for item in client.get("http://127.0.0.1:11434/api/ps").raise_for_status().json()["models"]:
            client.post(
                "http://127.0.0.1:11434/api/generate", json={"model": item["name"], "keep_alive": 0}
            ).raise_for_status()


def cleanup_containers(workspace):
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


def run(output):  # noqa: PLR0912, PLR0915 - sequential bounded experiment lifecycle
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    inputs = json.loads((LAB / "comparison-inputs.json").read_text())
    for task in inputs["tasks"]:
        if sha(task["intake"]) != task["intake_sha256"]:
            msg = "Frozen intake changed"
            raise ValueError(msg)
        for name, digest in task["source_files"].items():
            if sha(Path(task["source"]) / name) != digest:
                msg = "Frozen source changed"
                raise ValueError(msg)
    jobs = []
    for task in range(3):
        for rep in range(2):
            engines = ["ollama", "mlx"] if (task + rep) % 2 == 0 else ["mlx", "ollama"]
            for engine in engines:
                jobs.append(  # noqa: PERF401 - explicit paired order
                    {
                        "task": task,
                        "repetition": rep + 1,
                        "engine": engine,
                        "directory": str(output / f"task-{task + 1}-rep-{rep + 1}-{engine}"),
                    }
                )
    runtime_hashes = {
        str(p.relative_to(ROOT)): sha(p)
        for p in [
            *sorted((ROOT / "src").rglob("*.py")),
            Path(__file__).resolve(),
            ROOT / "scripts/mlx_lab.py",
            ROOT / "scripts/mlx-lab-requirements.txt",
        ]
    }
    with httpx.Client(timeout=10, trust_env=False) as client:
        ollama_identity = {
            "version": client.get("http://127.0.0.1:11434/api/version").raise_for_status().json(),
            "models": client.get("http://127.0.0.1:11434/api/tags").raise_for_status().json(),
        }
    plan = {
        "runtime_hashes": runtime_hashes,
        "ollama_identity": ollama_identity,
        "task_contracts": [
            make_task(t, i)[1].model_dump(mode="json") for i, t in enumerate(inputs["tasks"])
        ],
        "tasks": inputs["tasks"],
        "jobs": jobs,
        "seconds": SECONDS,
        "turns": TURNS,
        "output_tokens": OUTPUT,
        "sampling_seed": 42,
        "penalty_window": 64,
        "sandbox_max_open_files": 1024,
        "quantization_confound": "MLX affine 4-bit vs Ollama Q4_K_M",
        "quality_gate": "Real fixed and independent checks; no frontier source review",
        "model": mlx_lab.model_info(),
    }
    plan_path = output / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2))
    lease = ModelLease()
    lease.acquire("bounded-mlx-comparison")
    completed = []
    try:
        for index, job in enumerate(jobs):
            if any(sha(ROOT / name) != digest for name, digest in runtime_hashes.items()):
                msg_0 = "Comparison runtime changed; stopping before another trial"
                raise ValueError(msg_0)
            proc = provider = None
            started = time.monotonic()
            print(json.dumps({"event": "trial_start", "index": index, "job": job}), flush=True)
            try:
                unload_ollama()
                if job["engine"] == "mlx":
                    provider = mlx_lab.server(False, 18181)
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        if provider.poll() is not None:
                            msg_0 = "MLX server exited"
                            raise RuntimeError(msg_0)  # noqa: TRY301 - owned provider died
                        try:
                            if httpx.get(
                                "http://127.0.0.1:18181/v1/models", timeout=2, trust_env=False
                            ).is_success:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(1)
                    else:
                        msg_0 = "MLX startup timeout"
                        raise TimeoutError(msg_0)  # noqa: TRY301 - bounded readiness
                with (output / f"trial-{index:02}.log").open("w") as log:
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--child",
                            str(plan_path),
                            str(index),
                        ],
                        cwd=ROOT,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    try:
                        proc.wait(timeout=SECONDS)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait(timeout=5)
                result_path = Path(job["directory"]) / "result.json"
                result = (
                    json.loads(result_path.read_text())
                    if result_path.exists()
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
                cleanup_containers(Path(job["directory"]) / "workspace")
            completed.append({**job, **result, "total_wall_seconds": time.monotonic() - started})
            (output / "results.json").write_text(json.dumps(completed, indent=2))
            print(
                json.dumps(
                    {
                        "event": "trial_end",
                        "index": index,
                        "status": result["status"],
                        "passed": result.get("verified_success"),
                        "seconds": round(time.monotonic() - started, 2),
                    }
                ),
                flush=True,
            )
    finally:
        lease.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", nargs=2)
    args = parser.parse_args()

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    if args.child:
        child(args.child[0], int(args.child[1]))
    elif args.output:
        run(args.output)
    else:
        parser.error("--output is required")


if __name__ == "__main__":
    main()
