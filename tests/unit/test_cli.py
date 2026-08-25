from __future__ import annotations

import re
import runpy
import sys

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from loopforge.entrypoints.cli import main

DEMO_LINE = re.compile(r"run=run_[0-9a-f]{12} status=succeeded iterations=2 cost=\$0\.02")


def _argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["loopforge", *args])


def test_demo_command_completes_and_reports_succeeded_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "demo")

    assert main() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert DEMO_LINE.fullmatch(lines[0]) is not None


def test_demo_run_ids_are_unique_across_invocations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "demo")
    assert main() == 0
    first = capsys.readouterr().out.strip()

    _argv(monkeypatch, "demo")
    assert main() == 0
    second = capsys.readouterr().out.strip()

    first_run_id = first.split(" ", 1)[0]
    second_run_id = second.split(" ", 1)[0]
    assert first_run_id.startswith("run=run_")
    assert second_run_id.startswith("run=run_")
    assert first_run_id != second_run_id


def test_missing_command_exits_with_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: loopforge")
    assert "the following arguments are required: command" in captured.err


def test_unknown_command_exits_with_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "bogus")

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: loopforge")
    assert "invalid choice: 'bogus'" in captured.err


def test_extra_arguments_exit_with_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "demo", "extra")

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: loopforge")
    assert "unrecognized arguments: extra" in captured.err


def test_help_exits_zero_and_documents_demo_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "--help")

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("usage: loopforge")
    assert "{demo}" in captured.out


@example(command="DEMO")
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    command=st.from_regex(r"[A-Za-z0-9_.]{1,24}", fullmatch=True).filter(
        lambda token: token != "demo"
    )
)
def test_non_demo_command_always_exits_with_usage_error(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, command)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: loopforge")


def test_module_main_guard_runs_demo_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "demo")
    # Re-execute the module fresh so the __main__ guard sees __name__ == "__main__".
    monkeypatch.delitem(sys.modules, "loopforge.entrypoints.cli")

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("loopforge.entrypoints.cli", run_name="__main__")

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert DEMO_LINE.fullmatch(captured.out.strip()) is not None
