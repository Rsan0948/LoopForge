"""Zero-friction console launcher: packaged UI over a local SQLite store.

``loopforge console`` is the friendly front door: no flags, no npm, no
Postgres. It serves the prebuilt console UI that ships inside the wheel
(``loopforge/console_static``) — falling back to a repo checkout's
``ui/dist`` during development — over a SQLite event store under
``~/.loopforge/console``, picks a free loopback port, and opens a browser.

The console remains a trusted-operator local tool (D10): no authentication,
loopback bind only. This module never accepts a non-loopback host.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

_PACKAGE_STATIC = Path(__file__).resolve().parent.parent / "console_static"
"""Prebuilt console assets shipped inside the wheel."""

_REPO_STATIC = Path(__file__).resolve().parents[3] / "ui" / "dist"
"""Development fallback: a repo checkout's built UI (``cd ui && npx vite build``)."""

_LOOPBACK_HOST = "127.0.0.1"

_STARTUP_TIMEOUT_S = 15.0
"""How long to wait for the server to accept connections before giving up."""

_STARTUP_POLL_S = 0.05

MAX_START_ATTEMPTS = 3
"""Bind retries when the port was auto-picked (a picked port can lose the race)."""


def console_static_dir() -> Path | None:
    """Locate a usable built console UI, or ``None`` when none exists.

    Prefers the assets packaged inside the wheel; falls back to a source
    checkout's ``ui/dist`` so the command also works from a clone.
    """
    for candidate in (_PACKAGE_STATIC, _REPO_STATIC):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def console_data_dir() -> Path:
    """Operator-owned console state directory (event store, registry, profiles)."""
    return Path.home() / ".loopforge" / "console"


def find_free_port(host: str = _LOOPBACK_HOST) -> int:
    """Ask the OS for an unused port on ``host`` and return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _start_server(uvicorn: Any, app: Any, port: int) -> tuple[Any, threading.Thread]:
    """Start uvicorn on a daemon thread and wait until it accepts connections.

    Returns ``(server, thread)``; ``server.started`` tells the caller whether
    startup succeeded. Running uvicorn off the main thread lets us gate the
    browser open on actual readiness instead of racing it.
    """
    config = uvicorn.Config(app, host=_LOOPBACK_HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while thread.is_alive() and not server.started and time.monotonic() < deadline:
        time.sleep(_STARTUP_POLL_S)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
    return server, thread


def run_console(*, port: int | None = None, open_browser: bool = True) -> int:
    """Serve the console until interrupted. ``port=None`` picks a free one.

    The browser (and the printed URL) open only after the server is actually
    accepting connections; if an auto-picked port loses the bind race, a fresh
    port is retried automatically.
    """
    # Lazy imports: keep `loopforge --help` and the other commands free of the
    # server import chain (and avoid the cli <-> sessions import cycle).
    import uvicorn as uvicorn_module  # noqa: PLC0415

    from loopforge.entrypoints.server import ServerSettings, create_app  # noqa: PLC0415

    static_dir = console_static_dir()
    if static_dir is None:
        print(
            "error: console UI assets not found. This install is missing the "
            "packaged console; from a source checkout run `cd ui && npx vite build`.",
            file=sys.stderr,
        )
        return 2

    data_dir = console_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = ServerSettings(
        store_kind="sqlite",
        sqlite_path=str(data_dir / "events.db"),
        data_dir=data_dir,
        static_dir=static_dir,
    )
    app = create_app(settings)

    attempts = 1 if port is not None else MAX_START_ATTEMPTS
    server: Any = None
    thread: threading.Thread | None = None
    bind_port = port if port is not None else 0
    for attempt in range(attempts):
        if port is None:
            bind_port = find_free_port()
        server, thread = _start_server(uvicorn_module, app, bind_port)
        if server.started:
            break
        if attempt < attempts - 1:
            print(f"port {bind_port} was unavailable; trying another...", file=sys.stderr)
    if server is None or thread is None or not server.started:
        print(
            f"error: the console server could not start on {_LOOPBACK_HOST}. "
            "Re-run the command to try a fresh port.",
            file=sys.stderr,
        )
        return 1

    url = f"http://{_LOOPBACK_HOST}:{bind_port}/"
    print()
    print("  ┌───────────────────────────────────────────────┐")
    print(f"  │  LoopForge console: {url:<25} │")
    print("  │  (Ctrl-C to stop)                             │")
    print("  └───────────────────────────────────────────────┘")
    print(flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        thread.join()
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=5)
        print("\nconsole stopped.")
    return 0
