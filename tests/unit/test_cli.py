from __future__ import annotations

import re
import runpy
import subprocess
import sys

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from loopforge.entrypoints.cli import build_ollama_model, main
from loopforge.workloads.fixtures import adder_repair_task


def _rlimit_as_supported() -> bool:
    probe = "import resource; resource.setrlimit(resource.RLIMIT_AS, (268435456, 268435456))"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


_REQUIRES_RLIMIT_AS = pytest.mark.skipif(
    not _rlimit_as_supported(),
    reason=(
        "platform rejects setrlimit(RLIMIT_AS); local sandbox launcher cannot apply "
        "resource limits, so the trusted repair demo fails closed"
    ),
)

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
    assert DEMO_LINE.fullmatch(lines[0]) is not None


def test_demo_prints_causally_correlated_telemetry_narrative(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "demo")

    assert main() == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    run_id = DEMO_LINE.fullmatch(lines[0]).group(0).split(" ", 1)[0].removeprefix("run=")  # pyright: ignore[reportOptionalMemberAccess]

    assert "non-authoritative projection; event store is authoritative" in lines[1]
    sections = [line for line in lines if line in {"trace:", "logs:", "metrics:"}]
    assert sections == ["trace:", "logs:", "metrics:"]

    span_lines = [line for line in lines if line.startswith("  span ")]
    # One correlated trace: every span belongs to the demo run, and the run
    # root span closes the successful run.
    assert span_lines
    assert all(f"id={run_id}:span:" in line or f"id={run_id}:span:0" in line for line in span_lines)
    assert any(
        line.startswith("  span loopforge.run ") and "status=ok" in line for line in span_lines
    )
    assert any("span loopforge.cycle" in line for line in span_lines)
    assert any("span loopforge.tool.execute" in line and "action=a2" in line for line in span_lines)

    log_lines = [line for line in lines if line.startswith(("  info ", "  debug ", "  warn "))]
    assert all(f"run={run_id}" in line for line in log_lines)
    # The sensitive fix tool's observation is redacted before export, while the
    # internal inspect observation remains visible.
    assert any("loopforge.tool.observation=[redacted]" in line for line in log_lines)
    assert any("loopforge.tool.observation=tests still failing" in line for line in log_lines)

    metric_lines = [line for line in lines if line.startswith("  loopforge.")]
    assert any("loopforge.runs.completed counter=1.0" in line for line in metric_lines)
    assert any("loopforge.cycles counter=1.0" in line for line in metric_lines)


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


@_REQUIRES_RLIMIT_AS
def test_repair_demo_repairs_fixture_and_prints_exact_patch_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "repair-demo")

    assert main() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[0].startswith("run=run_")
    assert "status=succeeded" in lines[0]
    assert "command:run_tests: passed (exit_code=0)" in captured.out
    assert "evidence artifacts recorded: 2" in captured.out
    assert "exact patch evidence:" in captured.out
    assert "-    return left - right" in captured.out
    assert "+    return left + right" in captured.out


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
    assert "{demo,repair-demo}" in captured.out


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
    assert DEMO_LINE.fullmatch(captured.out.splitlines()[0]) is not None


# --- PACS-010 hardening: CLI input-validation pins ---


def test_repair_demo_rejects_empty_container_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "repair-demo", "--container", "")

    assert main() == 2

    captured = capsys.readouterr()
    assert "non-empty image reference" in captured.out


def test_repair_demo_rejects_flag_like_container_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The equals form forces argparse to accept the flag-like token as the value.
    _argv(monkeypatch, "repair-demo", "--container=--privileged")

    assert main() == 2

    captured = capsys.readouterr()
    assert "invalid repair-demo configuration" in captured.out
    assert "must not start with '-'" in captured.out


def test_repair_demo_rejects_unknown_model_choice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "repair-demo", "--model", "bogus")

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_repair_demo_rejects_empty_ollama_model_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "repair-demo", "--model", "ollama", "--ollama-model", "")

    assert main() == 2

    captured = capsys.readouterr()
    assert "invalid repair-demo configuration" in captured.out


def test_repair_demo_rejects_scheme_less_ollama_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "repair-demo", "--model", "ollama", "--ollama-url", "localhost:11434")

    assert main() == 2

    captured = capsys.readouterr()
    assert "invalid repair-demo configuration" in captured.out


def test_ollama_factory_passes_env_credential_to_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _RecordingModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("loopforge.entrypoints.cli.OllamaModel", _RecordingModel)
    monkeypatch.setenv("LOOPFORGE_OLLAMA_API_KEY", "token-123")
    build_ollama_model(
        adder_repair_task(), model_name="devstral-small-2:latest", base_url="http://localhost:9"
    )
    assert captured["api_key"] == "token-123"
    assert captured["model"] == "devstral-small-2:latest"


def test_ollama_factory_ignores_blank_env_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _RecordingModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("loopforge.entrypoints.cli.OllamaModel", _RecordingModel)
    monkeypatch.setenv("LOOPFORGE_OLLAMA_API_KEY", "   ")
    build_ollama_model(adder_repair_task(), model_name="m", base_url="http://localhost:11434")
    assert captured["api_key"] is None


def test_ollama_factory_registers_wiring_supplied_capability_metadata() -> None:
    model = build_ollama_model(
        adder_repair_task(),
        model_name="devstral-small-2:latest",
        base_url="http://localhost:11434",
        context_window_tokens=64_000,
    )
    try:
        capabilities = model.capabilities
        assert capabilities.provider == "ollama"
        assert capabilities.model == "devstral-small-2:latest"
        assert capabilities.supports_tool_calls is True
        assert capabilities.context_window_tokens == 64_000
    finally:
        model.close()


def test_repair_demo_rejects_invalid_ollama_context_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "repair-demo", "--model", "ollama", "--ollama-context-window", "0")

    assert main() == 2

    captured = capsys.readouterr()
    assert "invalid repair-demo configuration" in captured.out
