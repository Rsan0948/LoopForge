"""Local MLX lab setup and bounded smoke checks; no campaign or tool execution.

Use the existing LoopForge .venv to run this script. MLX dependencies live in a
separate .loopforge/mlx-lab/.venv; inference uses upstream mlx-lm unchanged.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx

from loopforge.adapters.model_lease import ModelLease
from loopforge.adapters.ollama_profiles import profile_for

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / ".loopforge/mlx-lab"
MODEL = "mlx-community/Qwen3.6-35B-A3B-4bit"
REVISION = "38740b847e4cb78f352aba30aa41c76e08e6eb46"
PORT = 18181


def model_info() -> dict:
    return json.loads((LAB / "model.json").read_text())


def status() -> None:
    result = {"python": str(LAB / ".venv/bin/python"), "download_complete": False}
    if (LAB / "model.json").exists():
        info = model_info()
        result |= {
            "download_complete": True,
            "model": info,
            "weights_present": all((Path(info["local_path"]) / f).is_file() for f in info["files"]),
        }
    result["smoke"] = (
        json.loads((LAB / "artifacts/smoke.json").read_text())
        if (LAB / "artifacts/smoke.json").exists()
        else None
    )
    print(json.dumps(result, indent=2))


def setup() -> None:
    LAB.mkdir(parents=True, exist_ok=True)
    (LAB / "artifacts").mkdir(exist_ok=True)
    subprocess.run(
        ["uv", "venv", "--python", "3.12", str(LAB / ".venv"), "--allow-existing"], check=True
    )
    python = str(LAB / ".venv/bin/python")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            python,
            "-r",
            str(ROOT / "scripts/mlx-lab-requirements.txt"),
        ],
        check=True,
    )
    # Official downloader verifies content-addressed cache objects and resumes.
    code = (
        "from huggingface_hub import snapshot_download; import json; "
        f"print(json.dumps(snapshot_download({MODEL!r},revision={REVISION!r},"
        "allow_patterns=['*.json','*.safetensors','*.jinja','*.txt','README.md'],max_workers=4)))"
    )
    out = subprocess.run([python, "-c", code], check=True, capture_output=True, text=True)
    path = Path(json.loads(out.stdout.strip().splitlines()[-1]))
    (LAB / "model.json").write_text(
        json.dumps(
            {
                "model_id": MODEL,
                "revision": REVISION,
                "local_path": str(path),
                "files": {f.name: f.stat().st_size for f in sorted(path.iterdir()) if f.is_file()},
                "quantization_confound": "MLX affine 4-bit versus Ollama GGUF Q4_K_M",
            },
            indent=2,
        )
    )
    freeze = subprocess.run(
        ["uv", "pip", "freeze", "--python", python], check=True, capture_output=True, text=True
    )
    (LAB / "artifacts/environment-freeze.txt").write_text(freeze.stdout)
    oracle = LAB / "renderer-oracle" / "renderer-oracle"
    oracle.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["go", "build", "-mod=readonly", "-o", str(oracle), "."],
        cwd=ROOT / "scripts/ollama_renderer",
        check=True,
    )
    status()


def server(
    unload_ollama: bool,
    port: int,
    chat_template: str | None = None,
    *,
    agentic_cache: bool = False,
    extra_args: tuple[str, ...] = (),
) -> subprocess.Popen:
    info = model_info()
    if info["revision"] != REVISION:
        msg = "Unexpected model revision"
        raise ValueError(msg)
    # Refuse occupied ports before unloading anything or launching inference.
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    with httpx.Client(timeout=15, trust_env=False) as client:
        try:
            loaded = client.get("http://127.0.0.1:11434/api/ps").raise_for_status().json()["models"]
        except httpx.ConnectError:
            loaded = []
        if loaded and not unload_ollama:
            msg = "Ollama has loaded models; use --unload-ollama for this exclusive lab run"
            raise ValueError(msg)
        for item in loaded:
            client.post(
                "http://127.0.0.1:11434/api/generate",
                json={
                    "model": item["name"],
                    "keep_alive": 0,
                },
            ).raise_for_status()
    env = os.environ.copy()
    env |= {
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    log = (LAB / "artifacts/server.log").open("a")
    try:
        return subprocess.Popen(
            [
                # agentic_cache: upstream server plus a cache checkpoint for
                # tool-result turns (see scripts/mlx_server_agentic.py).
                *(
                    [str(LAB / ".venv/bin/python"), str(ROOT / "scripts/mlx_server_agentic.py")]
                    if agentic_cache
                    else [str(LAB / ".venv/bin/mlx_lm.server")]
                ),
                "--model",
                info["local_path"],
                *(["--chat-template", chat_template] if chat_template is not None else []),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--max-tokens",
                "512",
                "--decode-concurrency",
                "1",
                "--prompt-concurrency",
                "1",
                "--prompt-cache-size",
                "2",
                "--prompt-cache-bytes",
                "4294967296",
                *extra_args,
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def smoke(port: int, proc: subprocess.Popen) -> None:  # noqa: PLR0915 - bounded protocol sequence
    deadline = time.monotonic() + 240
    with httpx.Client(timeout=5, trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                msg = "MLX exited; inspect .loopforge/mlx-lab/artifacts/server.log"
                raise RuntimeError(msg)
            try:
                if client.get(f"http://127.0.0.1:{port}/v1/models").is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            msg = "MLX startup exceeded 240 seconds"
            raise TimeoutError(msg)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file. This is a simulated smoke-test tool.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    messages = [
        {
            "role": "user",
            "content": "Call read_file exactly once with path README.md. Do not answer with prose.",
        }
    ]
    payload = {
        "model": model_info()["local_path"],
        "messages": messages,
        "tools": tools,
        "max_tokens": 128,
        "temperature": 0.0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    exchanges = []
    with httpx.Client(timeout=180, trust_env=False) as client:
        started = time.monotonic()
        response = client.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        exchanges.append(
            {"request": payload, "response": data, "seconds": time.monotonic() - started}
        )
        message = data["choices"][0]["message"]
        calls = message.get("tool_calls", [])
        if len(calls) != 1 or calls[0]["function"]["name"] != "read_file":
            msg = "Smoke test did not produce exactly one read_file tool call"
            raise ValueError(msg)
        if json.loads(calls[0]["function"]["arguments"]) != {"path": "README.md"}:
            msg = "Smoke tool arguments did not match"
            raise ValueError(msg)
        followup = {
            **payload,
            "messages": [
                *messages,
                message,
                {
                    "role": "tool",
                    "tool_call_id": calls[0]["id"],
                    "name": "read_file",
                    "content": "SIMULATED TOOL RESULT: The setup status is ready.",
                },
                {"role": "user", "content": "Reply with only the setup status word."},
            ],
        }
        started = time.monotonic()
        response = client.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=followup)
        response.raise_for_status()
        data = response.json()
        exchanges.append(
            {"request": followup, "response": data, "seconds": time.monotonic() - started}
        )
        if data["choices"][0]["message"].get("content", "").strip().lower().rstrip(".") != "ready":
            msg = "Smoke test did not consume the simulated tool result"
            raise ValueError(msg)
        profile = profile_for("coding", native=True, model="qwen3.6:35b-a3b")
        thinking = {
            **payload,
            "max_tokens": 512,
            "temperature": profile.temperature,
            "top_p": profile.top_p,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": profile.presence_penalty,
            "chat_template_kwargs": {"enable_thinking": True},
        }
        started = time.monotonic()
        response = client.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=thinking)
        response.raise_for_status()
        data = response.json()
        exchanges.append(
            {"request": thinking, "response": data, "seconds": time.monotonic() - started}
        )
        calls = data["choices"][0]["message"].get("tool_calls", [])
        if (
            len(calls) != 1
            or calls[0]["function"]["name"] != "read_file"
            or json.loads(calls[0]["function"]["arguments"]) != {"path": "README.md"}
            or data["choices"][0]["finish_reason"] == "length"
        ):
            (LAB / "artifacts/smoke-failed-exchanges.json").write_text(json.dumps(exchanges))
            msg = "Thinking-enabled tool call failed or exhausted its bounded output"
            raise ValueError(msg)
    result = {
        "passed": True,
        "model": model_info(),
        "exchanges": exchanges,
        "real_tool_execution": False,
        "quality_benchmark": False,
    }
    (LAB / "artifacts/smoke.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"passed": True, "exchanges": len(exchanges), "quality_benchmark": False}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "status", "serve", "smoke"])
    parser.add_argument("--unload-ollama", action="store_true")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    if args.command == "setup":
        setup()
        return
    if args.command == "status":
        status()
        return

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    lease = ModelLease()
    lease.acquire("mlx-lab-" + args.command)
    proc = None
    try:
        proc = server(args.unload_ollama, args.port)
        if args.command == "smoke":
            smoke(args.port, proc)
        else:
            print(f"MLX pid={proc.pid} on 127.0.0.1:{args.port}; Ctrl+C stops it", flush=True)
            proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None:
            stop_server(proc)
        lease.release()


if __name__ == "__main__":
    main()
