"""Unit tests for the zero-friction console launcher (entrypoints/console.py).

Rule 10 pairing: allow-paths (asset resolution order, free port, settings
wiring, browser open only after readiness, port retry) and deny-paths
(missing assets refuse to start; a server that never becomes ready exits 1
with no browser side effects).
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


def _fake_uvicorn(monkeypatch: pytest.MonkeyPatch, *, fail_starts: int = 0) -> list[dict[str, Any]]:
    """Patch uvicorn.Config/Server with in-process fakes; return captured configs.

    The first ``fail_starts`` server instances never reach ``started`` (their
    ``run`` exits immediately), simulating a lost bind race; later instances
    start successfully.
    """
    configs: list[dict[str, Any]] = []
    starts = {"seen": 0}

    class FakeConfig:
        def __init__(self, app: object, **kwargs: object) -> None:
            configs.append({"app": app, **kwargs})

    class FakeServer:
        def __init__(self, config: FakeConfig) -> None:
            self.config = config
            self.started = False
            self.should_exit = False

        def run(self) -> None:
            starts["seen"] += 1
            if starts["seen"] > fail_starts:
                self.started = True

    monkeypatch.setattr("uvicorn.Config", FakeConfig)
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    return configs


def _fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console, "_STARTUP_TIMEOUT_S", 0.3)
    monkeypatch.setattr(console, "_STARTUP_POLL_S", 0.01)


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
    def _isolate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, dict[str, Any], list[str]]:
        static = _make_ui(tmp_path / "pkg")
        monkeypatch.setattr(console, "_PACKAGE_STATIC", static)
        monkeypatch.setattr(console, "_REPO_STATIC", tmp_path / "missing-repo")
        monkeypatch.setattr(console, "console_data_dir", lambda: tmp_path / "state")

        captured: dict[str, Any] = {}

        def _create_app(settings: server_module.ServerSettings) -> object:
            captured["settings"] = settings
            return object()

        def _open(url: str) -> None:
            opened.append(url)

        opened: list[str] = []
        monkeypatch.setattr(server_module, "create_app", _create_app)
        monkeypatch.setattr(console.webbrowser, "open", _open)
        return static, captured, opened

    def test_serves_packaged_ui_over_local_sqlite_after_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        static, captured, opened = self._isolate(tmp_path, monkeypatch)
        configs = _fake_uvicorn(monkeypatch)

        assert console.run_console() == 0

        settings = captured["settings"]
        assert isinstance(settings, server_module.ServerSettings)
        assert settings.store_kind == "sqlite"
        assert settings.sqlite_path == str(tmp_path / "state" / "events.db")
        assert settings.data_dir == tmp_path / "state"
        assert settings.static_dir == static
        assert len(configs) == 1
        assert configs[0]["host"] == "127.0.0.1"
        port = configs[0]["port"]
        assert isinstance(port, int)
        assert 1024 <= port <= 65535
        assert opened == [f"http://127.0.0.1:{port}/"]

    def test_explicit_port_and_no_browser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, opened = self._isolate(tmp_path, monkeypatch)
        configs = _fake_uvicorn(monkeypatch)

        assert console.run_console(port=9876, open_browser=False) == 0
        assert configs[0]["port"] == 9876
        assert opened == []

    def test_retries_a_fresh_port_when_the_first_loses_the_race(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, opened = self._isolate(tmp_path, monkeypatch)
        _fast_timeout(monkeypatch)
        configs = _fake_uvicorn(monkeypatch, fail_starts=1)

        assert console.run_console() == 0
        assert len(configs) == 2
        assert configs[0]["port"] != configs[1]["port"]
        assert opened == [f"http://127.0.0.1:{configs[1]['port']}/"]

    def test_exits_1_without_opening_a_browser_when_startup_never_readies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, _, opened = self._isolate(tmp_path, monkeypatch)
        _fast_timeout(monkeypatch)
        configs = _fake_uvicorn(monkeypatch, fail_starts=99)

        assert console.run_console() == 1
        assert len(configs) == console.MAX_START_ATTEMPTS
        assert opened == []
        assert "could not start" in capsys.readouterr().err

    def test_explicit_port_failure_does_not_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, opened = self._isolate(tmp_path, monkeypatch)
        _fast_timeout(monkeypatch)
        configs = _fake_uvicorn(monkeypatch, fail_starts=99)

        assert console.run_console(port=9876) == 1
        assert len(configs) == 1
        assert opened == []

    def test_missing_assets_refuse_to_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(console, "_PACKAGE_STATIC", tmp_path / "missing-pkg")
        monkeypatch.setattr(console, "_REPO_STATIC", tmp_path / "missing-repo")

        def fail_open(url: str) -> None:
            pytest.fail(f"browser must not open: {url}")

        monkeypatch.setattr(console.webbrowser, "open", fail_open)

        assert console.run_console() == 2
        assert "console UI assets not found" in capsys.readouterr().err
