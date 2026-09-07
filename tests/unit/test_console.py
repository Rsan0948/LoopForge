"""Unit tests for the zero-friction console launcher (entrypoints/console.py).

Rule 10 pairing: allow-paths (asset resolution order, free port, settings
wiring, browser open) and deny-paths (missing assets refuse to start, no
server/browser side effects on the denial).
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from loopforge.entrypoints import console
from loopforge.entrypoints import server as server_module


def _make_ui(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    return root


class TestConsoleStaticDir:
    def test_prefers_packaged_assets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        packaged = _make_ui(tmp_path / "pkg")
        repo = _make_ui(tmp_path / "repo")
        monkeypatch.setattr(console, "_PACKAGE_STATIC", packaged)
        monkeypatch.setattr(console, "_REPO_STATIC", repo)
        assert console.console_static_dir() == packaged

    def test_falls_back_to_repo_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _make_ui(tmp_path / "repo")
        monkeypatch.setattr(console, "_PACKAGE_STATIC", tmp_path / "missing-pkg")
        monkeypatch.setattr(console, "_REPO_STATIC", repo)
        assert console.console_static_dir() == repo

    def test_requires_index_html(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(console, "_PACKAGE_STATIC", empty)
        monkeypatch.setattr(console, "_REPO_STATIC", tmp_path / "missing-repo")
        assert console.console_static_dir() is None


class TestFindFreePort:
    def test_returns_bindable_port(self) -> None:
        port = console.find_free_port()
        assert 1024 <= port <= 65535
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))  # must still be free after we let it go


class TestRunConsole:
    def test_serves_packaged_ui_over_local_sqlite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        static = _make_ui(tmp_path / "pkg")
        monkeypatch.setattr(console, "_PACKAGE_STATIC", static)
        monkeypatch.setattr(console, "_REPO_STATIC", tmp_path / "missing-repo")
        monkeypatch.setattr(console, "console_data_dir", lambda: tmp_path / "state")

        captured: dict[str, Any] = {}

        def fake_create_app(settings: server_module.ServerSettings) -> object:
            captured["settings"] = settings
            return object()

        def fake_run(app: object, **kwargs: object) -> None:
            captured["app"] = app
            captured["kwargs"] = kwargs

        opened: list[str] = []

        def _open(url: str) -> None:
            opened.append(url)

        monkeypatch.setattr(server_module, "create_app", fake_create_app)
        monkeypatch.setattr("uvicorn.run", fake_run)
        monkeypatch.setattr(console.webbrowser, "open", _open)

        assert console.run_console() == 0

        settings = captured["settings"]
        assert isinstance(settings, server_module.ServerSettings)
        assert settings.store_kind == "sqlite"
        assert settings.sqlite_path == str(tmp_path / "state" / "events.db")
        assert settings.data_dir == tmp_path / "state"
        assert settings.static_dir == static
        kwargs = captured["kwargs"]
        assert kwargs["host"] == "127.0.0.1"
        port = kwargs["port"]
        assert isinstance(port, int)
        assert 1024 <= port <= 65535
        assert opened == [f"http://127.0.0.1:{port}/"]

    def test_explicit_port_and_no_browser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        static = _make_ui(tmp_path / "pkg")
        monkeypatch.setattr(console, "_PACKAGE_STATIC", static)
        monkeypatch.setattr(console, "_REPO_STATIC", tmp_path / "missing-repo")
        monkeypatch.setattr(console, "console_data_dir", lambda: tmp_path / "state")

        captured: dict[str, Any] = {}

        def fake_run(app: object, **kwargs: object) -> None:
            captured["kwargs"] = kwargs

        def fail_open(url: str) -> None:
            pytest.fail(f"browser must not open: {url}")

        def _create_app(settings: server_module.ServerSettings) -> object:
            return object()

        monkeypatch.setattr(server_module, "create_app", _create_app)
        monkeypatch.setattr("uvicorn.run", fake_run)
        monkeypatch.setattr(console.webbrowser, "open", fail_open)

        assert console.run_console(port=9876, open_browser=False) == 0
        assert captured["kwargs"]["port"] == 9876

    def test_missing_assets_refuse_to_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(console, "_PACKAGE_STATIC", tmp_path / "missing-pkg")
        monkeypatch.setattr(console, "_REPO_STATIC", tmp_path / "missing-repo")

        def fail_run(app: object, **kwargs: object) -> None:
            pytest.fail("server must not start without console assets")

        def fail_open(url: str) -> None:
            pytest.fail(f"browser must not open: {url}")

        monkeypatch.setattr("uvicorn.run", fail_run)
        monkeypatch.setattr(console.webbrowser, "open", fail_open)

        assert console.run_console() == 2
        assert "console UI assets not found" in capsys.readouterr().err
