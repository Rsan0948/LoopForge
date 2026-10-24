"""Replay one proven repair boundary without rewriting its context or settings.

Uses archived native requests, the original contract, strict Ollama parser,
registered coding tools and independent checks. No supervisor or paid calls.
Each replay permits one model action; this is not an autonomous full-task eval.
"""

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.model_lease import ModelLease
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.scripted import ScriptedModel
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import ActionProposed
from loopforge.domain.types import ActionId, RunStatus
from loopforge.entrypoints import campaign_coding as coding
from loopforge.entrypoints.campaign_models import CodingTaskConfig
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.ports.model import ModelFailureClass, ModelTurnError
from scripts import mlx_lab
from scripts.parity_checkpoint import CheckpointOllamaModel
from scripts.run_mlx_comparison import mlx_request, native_response, unload_ollama

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".loopforge/tasks/reconciliation-forced-attempt/review-coverage-2026-10-23"
CAPTURED_SHA = "b3503ee35a142224b348c9cfb6556c62355fcdb235f5799ac88b89548a51a7e6"
WATCHDOG = 600
# Preserve upstream parser auto-detection without adding a byte to the prompt.
RENDER_TEMPLATE = (ROOT / "scripts/ollama_renderer/chat_template.jinja").read_text()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_verified_capture(path: Path, expected_digest: str) -> dict[str, object]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        msg = "Captured request changed; refusing context substitution"
        raise ValueError(msg)
    data = json.loads(raw)
    if not isinstance(data, dict):
        msg = "Captured request must be an object"
        raise TypeError(msg)
    return data


def inputs():
    config = RepositoryGoalConfig.model_validate_json(
        (SOURCE / "repository-intake.json").read_text()
    )
    with sqlite3.connect(f"file:{SOURCE / 'worker/campaigns.sqlite'}?mode=ro", uri=True) as db:
        row = db.execute("SELECT id,config FROM coding_tasks").fetchone()
        task = CodingTaskConfig.model_validate_json(row[1])
    captured = SOURCE / "worker-prompts" / (CAPTURED_SHA + ".json")
    native = read_verified_capture(captured, CAPTURED_SHA)
    assert len(native["messages"]) == 32
    assert native["options"]["num_predict"] == 32768
    # Strict conservative byte bound for the actual archived wire packet.
    assert (
        len(json.dumps(native, ensure_ascii=False).encode()) + 32768 + 1024
        <= native["options"]["num_ctx"]
    )
    with sqlite3.connect(f"file:{SOURCE / 'worker/runs.sqlite'}?mode=ro", uri=True) as db:
        edits = []
        for (payload,) in db.execute(
            "SELECT payload FROM events WHERE event_type='ActionProposed' ORDER BY rowid"
        ):
            event = json.loads(payload)["event"]
            p = event["proposal"]
            if event["run_id"] == "run_56a6a108695d" and p["tool_name"] == "edit_file":
                edits.append(p)
    assert len(edits) == 2
    # At this boundary, the latest observed API file is exactly the prior edit.
    assert edits[0]["arguments"]["new"] == edits[1]["arguments"]["old"]
    return config, task, captured, native, edits


def boundary_workspace(out, config, task, edits):
    ws = coding.prepare_checkout(
        Path(config.campaign.repository), config.campaign.revision, out / "workspace", task.branch
    )
    args = edits[0]["arguments"]
    p = ws.root / args["path"]
    text = p.read_text()
    assert text.count(args["old"]) == 1
    p.write_text(text.replace(args["old"], args["new"], 1))
    assert p.read_text().count(edits[1]["arguments"]["old"]) == 1
    _, _, _, native, _ = inputs()
    assert "sha256=" + digest(p) in native["messages"][-1]["content"], "Boundary source differs"
    return ws


def assert_context_parity(native: dict[str, object], mapped: dict[str, object]) -> None:
    assert len(native["messages"]) == len(mapped["messages"]), "Message count changed"
    for a, b in zip(native["messages"], mapped["messages"], strict=True):
        assert a["role"] == b["role"], "Message role changed"
        assert a.get("content", "") == b.get("content", ""), "Message content changed"
        calls_a, calls_b = a.get("tool_calls", []), b.get("tool_calls", [])
        assert len(calls_a) == len(calls_b), "Historical call count changed"
        for x, y in zip(calls_a, calls_b, strict=True):
            assert x["function"]["name"] == y["function"]["name"], "Historical tool changed"
            assert x["function"].get("arguments", {}) == json.loads(y["function"]["arguments"]), (
                "Historical arguments changed"
            )
        if a["role"] == "tool":
            assert a.get("tool_name", a.get("name")) == b["name"], "Tool result name changed"
        if a.get("thinking"):
            assert a["thinking"] == b.get("reasoning_content"), "Historical thinking changed"


class ReplayTransport(httpx.BaseTransport):
    def __init__(  # noqa: PLR0913 - frozen adapter settings
        self, engine, out, native, deadline, *, continuation=False, official_renderer=False
    ):
        self.engine, self.out, self.native, self.deadline = engine, out, native, deadline
        self.calls = 0
        self.continuation = continuation
        self.official_renderer = official_renderer
        self.client = httpx.Client(trust_env=False)

    def handle_request(self, request):
        self.calls += 1
        if self.calls > 1 and not self.continuation:
            raise ModelTurnError(
                ModelFailureClass.PERMANENT,
                "REPLAY_BOUNDARY_COMPLETE",
                "One captured decision was evaluated; no further inference authorized",
            )
        generated = json.loads(request.content)
        # Existing runtime must expose the same system prompt/tool schemas/settings.
        assert generated["messages"][0] == self.native["messages"][0], "System prompt differs"
        assert generated["tools"] == self.native["tools"], (
            "Registered tools differ from successful run"
        )
        assert generated["options"] == self.native["options"], (
            "Inference options differ from successful run"
        )
        if self.continuation and self.calls == 1:
            assert generated == self.native, "Restored first request differs from captured request"
        actual = generated if self.continuation else self.native
        if self.engine == "ollama":
            payload = json.loads(json.dumps(actual))
            assert payload == actual
            url = "http://127.0.0.1:11434/api/chat"
        else:
            payload = mlx_request(actual, mlx_lab.model_info()["local_path"])
            assert payload["max_tokens"] == 32768
            assert payload["tools"] == self.native["tools"]
            assert_context_parity(actual, payload)
            if self.official_renderer:
                oracle = ROOT / ".loopforge/mlx-lab/renderer-oracle/renderer-oracle"
                rendered = subprocess.run(
                    [str(oracle)],
                    input=json.dumps(actual),
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=15,
                )
                prompt = json.loads(rendered.stdout)["prompt"]
                payload["chat_template_kwargs"]["ollama_rendered_prompt"] = prompt
            url = "http://127.0.0.1:18181/v1/chat/completions"
        record = {
            "engine": self.engine,
            "native_request": actual,
            "wire_request": payload,
            "context_parity": "exact captured native request; MLX API framing converted only",
            "captured_options_unchanged": True,
            "official_ollama_renderer": self.official_renderer,
        }
        dest = self.out / f"exchange-{self.calls:03}.json"
        dest.write_text(json.dumps(record))
        started = time.monotonic()
        try:
            response = self.client.post(url, json=payload, timeout=max(1, self.deadline - started))
            record |= {
                "http_status": response.status_code,
                "wall_seconds": time.monotonic() - started,
            }
            if not response.is_success:
                return httpx.Response(response.status_code, content=response.content)
            data = response.json()
            record["response"] = data
            normalized = native_response(data) if self.engine == "mlx" else data
            return httpx.Response(200, json=normalized)
        finally:
            dest.write_text(json.dumps(record))

    def close(self):
        self.client.close()


def execute(out, engine, continuation=False, official_renderer=False):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    config, task, captured, native, edits = inputs()
    ws = boundary_workspace(out, config, task, edits)
    (out / "task.json").write_text(task.model_dump_json(indent=2))
    (out / "captured-request.json").write_text(captured.read_text())
    (out / "boundary.patch").write_text(ws.diff())
    store = SQLiteEventStore(out / "events.sqlite", codec=JsonEventCodec())
    live = None
    started = time.monotonic()
    original = coding.OllamaModel
    result = {
        "engine": engine,
        "request_sha256": digest(captured),
        "task_sha256": digest(out / "task.json"),
    }
    transport = None
    try:
        if engine == "reference":
            p = edits[1]
            model = ScriptedModel(
                [
                    ActionProposal(
                        ActionId("recorded-successful-edit"), p["tool_name"], p["arguments"]
                    )
                ]
            )
            runtime, _ = coding.build_coding(config.campaign, task, ws, store=store, model=model)
        else:
            transport = ReplayTransport(
                engine,
                out,
                native,
                started + (1200 if continuation else WATCHDOG),
                continuation=continuation,
                official_renderer=official_renderer,
            )

            def factory(**kwargs):
                model = (CheckpointOllamaModel if continuation else OllamaModel)(
                    **kwargs, transport=transport
                )
                if continuation:
                    model.restore_checkpoint(native)
                return model

            coding.OllamaModel = factory
            runtime, live = coding.build_coding(config.campaign, task, ws, store=store)
            coding.OllamaModel = original
        run = runtime.start(task.objective)
        state = runtime.resume(run)
        final = runtime.verifier.verify(state)
        actions = [
            {"tool": e.proposal.tool_name, "arguments": dict(e.proposal.arguments)}
            for e in store.events_for(run)
            if isinstance(e, ActionProposed)
        ]
        result |= {
            "status": state.status.value,
            "verified_repair": state.status == RunStatus.SUCCEEDED and final.passed,
            "final_checks_passed": final.passed,
            "checks": final.summary,
            "actions": actions,
            "tokens_in": state.input_tokens,
            "tokens_out": state.output_tokens,
        }
        (out / "candidate.patch").write_text(ws.diff())
    except Exception as exc:
        result |= {"status": "error", "verified_repair": False, "error": str(exc)}
        raise
    finally:
        coding.OllamaModel = original
        if live:
            live.close()
        result["total_seconds"] = time.monotonic() - started
        (out / "result.json").write_text(json.dumps(result, indent=2))
        print(
            json.dumps({k: v for k, v in result.items() if k not in {"checks", "actions"}}),
            flush=True,
        )


def run(output, continuation=False, official_renderer=False, mlx_only=False, ollama_only=False):  # noqa: PLR0912, PLR0915 - bounded sequential parity experiment
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    _config, task, captured, native, _edits = inputs()
    # Store parity proof before any model inference; original files stay untouched.
    manifest = {
        "source": str(SOURCE),
        "request": str(captured),
        "request_sha256": digest(captured),
        "messages": len(native["messages"]),
        "options": native["options"],
        "think": native["think"],
        "task": task.model_dump(mode="json"),
        "mlx_model": mlx_lab.model_info(),
        "one_action_per_replay": not continuation,
        "normal_worker_continuation": continuation,
        "official_ollama_renderer_for_mlx": official_renderer,
        "control_artifact": str(
            ROOT / ".loopforge/mlx-lab/runs/native-continuation-2026-10-23/rep-1-ollama/result.json"
        )
        if mlx_only
        else None,
        "renderer_proof": json.loads(
            (ROOT / ".loopforge/mlx-lab/artifacts/official-renderer-parity.json").read_text()
        )
        if official_renderer
        else None,
        "wall_watchdog_seconds": 1200 if continuation else WATCHDOG,
        "quantization_confound": "MLX affine 4-bit vs Ollama Q4_K_M",
        "runtime_files": {
            str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / "src").rglob("*.py"))
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    lease = ModelLease()
    lease.acquire("successful-repair-parity-replay")
    try:
        # Replay known historical successful output first: no inference.
        execute(output / "reference", "reference", continuation)
        assert json.loads((output / "reference/result.json").read_text())["verified_repair"]
        results = []
        jobs = (
            [(1, "mlx"), (2, "mlx")]
            if mlx_only
            else [(1, "ollama"), (1, "mlx"), (2, "mlx"), (2, "ollama")]
        )
        if ollama_only:
            jobs = [(2, "ollama")]
        for rep, engine in jobs:
            provider = child = None
            out = output / f"rep-{rep}-{engine}"
            print(json.dumps({"event": "start", "rep": rep, "engine": engine}), flush=True)
            try:
                unload_ollama()
                if engine == "mlx":
                    provider = mlx_lab.server(
                        False, 18181, RENDER_TEMPLATE if official_renderer else None
                    )
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        if provider.poll() is not None:
                            msg = "MLX server exited"
                            raise RuntimeError(msg)
                        try:
                            if httpx.get(
                                "http://127.0.0.1:18181/v1/models", timeout=2, trust_env=False
                            ).is_success:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(1)
                    else:
                        msg = "MLX startup timeout"
                        raise TimeoutError(msg)
                with (output / f"rep-{rep}-{engine}.log").open("w") as log:
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--child",
                            str(out),
                            engine,
                            *(["--continue-worker"] if continuation else []),
                            *(["--ollama-renderer"] if official_renderer else []),
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=ROOT,
                        start_new_session=True,
                    )
                    try:
                        child.wait(timeout=1200 if continuation else WATCHDOG)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)
                result = (
                    json.loads((out / "result.json").read_text())
                    if (out / "result.json").exists()
                    else {"engine": engine, "verified_repair": False, "status": "watchdog_or_crash"}
                )
                result["rep"] = rep
                results.append(result)
                (output / "results.json").write_text(json.dumps(results, indent=2))
                print(
                    json.dumps(
                        {
                            "event": "result",
                            "rep": rep,
                            "engine": engine,
                            "status": result["status"],
                            "verified_repair": result["verified_repair"],
                        }
                    ),
                    flush=True,
                )
            finally:
                if child and child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
                if provider:
                    mlx_lab.stop_server(provider)
                unload_ollama()
    finally:
        lease.release()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path)
    p.add_argument("--child", nargs=2)
    p.add_argument("--continue-worker", action="store_true")
    p.add_argument("--ollama-renderer", action="store_true")
    p.add_argument("--mlx-only", action="store_true")
    p.add_argument("--ollama-only", action="store_true")
    a = p.parse_args()

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    if a.child:
        execute(Path(a.child[0]), a.child[1], a.continue_worker, a.ollama_renderer)
    elif a.output:
        run(a.output, a.continue_worker, a.ollama_renderer, a.mlx_only, a.ollama_only)
    else:
        p.error("--output is required")


if __name__ == "__main__":
    main()
