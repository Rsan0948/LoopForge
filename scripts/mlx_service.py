"""Run the pinned MLX server as a per-user launchd service, like the Ollama one.

The command is the lab's exact invocation (`mlx_lab.server_command`): the agentic cache
wrapper, Ollama's rendered-prompt chat template and the tuned default server settings.
Loopback only. The model loads at startup, so the service holds about 20 GB of unified
memory while it runs; `uninstall` frees it.

Usage: .venv/bin/python scripts/mlx_service.py install|uninstall|status
"""

import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlx_lab

LABEL = "local.loopforge.mlx"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
TEMPLATE = (mlx_lab.ROOT / "scripts/ollama_renderer/chat_template.jinja").read_text()
RENDERER = mlx_lab.LAB / "renderer-oracle/renderer-oracle"
URL = f"http://127.0.0.1:{mlx_lab.PORT}/v1/models"


def domain() -> str:
    return f"gui/{os.getuid()}"


def install() -> None:
    if mlx_lab.model_info()["revision"] != mlx_lab.REVISION:
        msg = "unexpected model revision"
        raise SystemExit(msg)
    if not RENDERER.is_file():
        msg = f"renderer missing; build scripts/ollama_renderer into {RENDERER}"
        raise SystemExit(msg)
    log = mlx_lab.LAB / "artifacts/server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": mlx_lab.server_command(mlx_lab.PORT, TEMPLATE, agentic_cache=True),
        "EnvironmentVariables": mlx_lab.SERVER_ENV,
        "WorkingDirectory": str(mlx_lab.ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    if PLIST.exists():
        subprocess.run(["launchctl", "bootout", domain(), str(PLIST)], check=False)
    PLIST.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", domain(), str(PLIST)], check=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if ready():
            print(json.dumps({"installed": str(PLIST), "ready": True}))
            return
        time.sleep(2)
    msg = f"installed, but the server did not become ready in 300 s; see {log}"
    raise SystemExit(msg)


def uninstall() -> None:
    if PLIST.exists():
        subprocess.run(["launchctl", "bootout", domain(), str(PLIST)], check=False)
        PLIST.unlink()
    print(json.dumps({"uninstalled": str(PLIST)}))


def ready() -> bool:
    try:
        return httpx.get(URL, timeout=2, trust_env=False).is_success
    except httpx.HTTPError:
        return False


def status() -> None:
    print(
        json.dumps(
            {"plist": PLIST.exists(), "ready": ready(), "renderer": RENDERER.is_file()}, indent=2
        )
    )


if __name__ == "__main__":
    {"install": install, "uninstall": uninstall, "status": status}[sys.argv[1]]()
