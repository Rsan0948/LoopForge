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
import webbrowser
from pathlib import Path

_PACKAGE_STATIC = Path(__file__).resolve().parent.parent / "console_static"
"""Prebuilt console assets shipped inside the wheel."""

_REPO_STATIC = Path(__file__).resolve().parents[3] / "ui" / "dist"
"""Development fallback: a repo checkout's built UI (``cd ui && npx vite build``)."""

_LOOPBACK_HOST = "127.0.0.1"


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


def run_console(*, port: int | None = None, open_browser: bool = True) -> int:
    """Serve the console until interrupted. ``port=None`` picks a free one."""
    # Lazy imports: keep `loopforge --help` and the other commands free of the
    # server import chain (and avoid the cli <-> sessions import cycle).
    import uvicorn  # noqa: PLC0415

    from loopforge.entrypoints.server import ServerSettings, create_app  # noqa: PLC0415

    static_dir = console_static_dir()
    if static_dir is None:
        print(
            "error: console UI assets not found. This install is missing the "
            "packaged console; from a source checkout run `cd ui && npx vite build`.",
            file=sys.stderr,
        )
        return 2

    bind_port = find_free_port() if port is None else port
    data_dir = console_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = ServerSettings(
        store_kind="sqlite",
        sqlite_path=str(data_dir / "events.db"),
        data_dir=data_dir,
        static_dir=static_dir,
    )
    url = f"http://{_LOOPBACK_HOST}:{bind_port}/"
    print()
    print("  ┌───────────────────────────────────────────────┐")
    print(f"  │  LoopForge console: {url:<25} │")
    print("  │  (Ctrl-C to stop)                             │")
    print("  └───────────────────────────────────────────────┘")
    print()
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(create_app(settings), host=_LOOPBACK_HOST, port=bind_port, log_level="warning")
    return 0
