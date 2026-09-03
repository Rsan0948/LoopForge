from __future__ import annotations

import re
import runpy
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from loopforge.adapters.scripted import FixedClock
from loopforge.domain.benchmarks import BenchmarkReport, ConfigReport
from loopforge.entrypoints.cli import build_ollama_model, main
from loopforge.entrypoints.eval import EvalReportStore
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
_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git executable unavailable; eval trials materialize fixtures with the Git CLI",
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
    # PACS-016 M8: the demo's read turn skips verification under the tuned
    # default, so only the workspace-changing write records patch evidence.
    assert "evidence artifacts recorded: 1" in captured.out
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
    for command in ("demo", "repair-demo", "orchestrated-repair-demo", "civicml-loop", "loop"):
        assert command in captured.out


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


# --- PACS-013: orchestrated-repair-demo pins ---


@_REQUIRES_RLIMIT_AS
def test_orchestrated_repair_demo_repairs_calculator_fixture_with_two_workers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "orchestrated-repair-demo")

    assert main() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[0].startswith("run=run_")
    assert "status=succeeded" in lines[0]
    assert "stop_reason=success_verified" in captured.out
    assert "worker=adder" in captured.out
    assert "worker=greeter" in captured.out
    assert captured.out.count("outcome=succeeded merge=merged") == 2
    assert "command:run_tests: passed (exit_code=0)" in captured.out
    assert "evidence artifacts recorded: 1" in captured.out


def test_orchestrated_repair_demo_rejects_empty_container_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "orchestrated-repair-demo", "--container", "")

    assert main() == 2

    captured = capsys.readouterr()
    assert "non-empty image reference" in captured.out


def test_orchestrated_repair_demo_rejects_flag_like_container_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The equals form forces argparse to accept the flag-like token as the value.
    _argv(monkeypatch, "orchestrated-repair-demo", "--container=--privileged")

    assert main() == 2

    captured = capsys.readouterr()
    assert "invalid orchestrated-repair-demo configuration" in captured.out
    assert "must not start with '-'" in captured.out


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


def _loop_profile_repo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").touch()
    return root


def _loop_profile(tmp_path: Path) -> str:
    repo = _loop_profile_repo(tmp_path / "repo")
    profile = tmp_path / "profile.toml"
    profile.write_text(
        f"""
[task]
id = "cli-loop"
objective = "Fix the failing checks."
repository = "{repo}"

[[checks]]
name = "unit_tests"
kind = "TEST"
argv = ["{{python}}", "-m", "pytest", "-q", "tests/unit"]
timeout_seconds = 300

[acceptance]
required = ["unit_tests"]
allowed_prefixes = ["src", "tests"]

[model]
provider = "scripted"
tier = "economy"

[budget]
max_cost_usd = 5.0
max_iterations = 30
""",
        encoding="utf-8",
    )
    return str(profile)


def test_loop_command_requires_a_profile_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _argv(monkeypatch, "loop")

    assert main() == 2

    captured = capsys.readouterr()
    assert "requires a profile" in captured.out


def test_loop_command_rejects_invalid_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _argv(monkeypatch, "loop", str(tmp_path / "missing.toml"))

    assert main() == 2

    captured = capsys.readouterr()
    assert "invalid loop profile" in captured.out


def test_loop_dry_run_prints_resolved_profile_without_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _argv(monkeypatch, "loop", _loop_profile(tmp_path), "--dry-run")

    assert main() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert "profile task=cli-loop" in out
    assert "mode=local" in out
    assert "check unit_tests kind=test" in out
    assert "acceptance required=['unit_tests']" in out
    assert "model provider=scripted" in out
    assert "budget max_cost=$5.00 max_iterations=30" in out


# --- PACS-016 (M6): eval command pins ---


def test_eval_list_reports_empty_results_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _argv(monkeypatch, "eval", "--list", "--results-dir", str(tmp_path))

    assert main() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"no eval reports in {tmp_path}" in captured.out


def test_eval_show_unknown_report_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _argv(monkeypatch, "eval", "--show", "eval-missing", "--results-dir", str(tmp_path))

    assert main() == 1

    captured = capsys.readouterr()
    assert "unknown eval report 'eval-missing'" in captured.out


def test_eval_rejects_unknown_task_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _argv(monkeypatch, "eval", "--tasks", "nope-task", "--results-dir", str(tmp_path))

    assert main() == 2

    captured = capsys.readouterr()
    assert "unknown benchmark task_id 'nope-task'" in captured.out


def test_eval_rejects_unknown_configuration_preset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _argv(
        monkeypatch,
        "eval",
        "--tasks",
        "bench-simple-bug",
        "--config",
        "bogus",
        "--results-dir",
        str(tmp_path),
    )

    assert main() == 2

    captured = capsys.readouterr()
    assert "unknown eval configuration preset 'bogus'" in captured.out


def test_eval_rejects_zero_trials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _argv(monkeypatch, "eval", "--trials", "0", "--results-dir", str(tmp_path))

    assert main() == 2

    captured = capsys.readouterr()
    assert "--trials must be at least 1" in captured.out


def test_eval_rejects_non_eval_model_choice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # argparse accepts deepseek globally; eval supports scripted|ollama only.
    _argv(monkeypatch, "eval", "--model", "deepseek", "--results-dir", str(tmp_path))

    assert main() == 2

    captured = capsys.readouterr()
    assert "eval supports --model scripted|ollama" in captured.out


def test_eval_container_task_without_image_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # CONTAINER-mode tasks never fall back to the trusted path (rule 13): the
    # eval aborts loudly instead of running an unsandboxed trial.
    _argv(
        monkeypatch,
        "eval",
        "--tasks",
        "bench-simple-bug",
        "--trials",
        "1",
        "--results-dir",
        str(tmp_path),
    )

    assert main() == 1

    captured = capsys.readouterr()
    assert "eval trial failed" in captured.out
    assert "no container image was wired" in captured.out
    assert list(tmp_path.glob("*.json")) == []


def test_eval_unexpected_driver_failure_is_a_clean_error_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """M9 W8: unexpected exceptions never leak internals to the terminal.

    ``EvalTrialError`` keeps its honest message (pinned by the container
    fail-closed test above); any OTHER driver/runner exception maps to a
    clean exit-1 line (PACS-011 CLI-leak precedent) — no raw traceback, no
    wiring detail or paths.
    """

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = "internals: /secret/wiring/path"
        raise RuntimeError(msg)

    monkeypatch.setattr("loopforge.entrypoints.cli.run_trials", boom)
    _argv(
        monkeypatch,
        "eval",
        "--tasks",
        "bench-simple-bug",
        "--trials",
        "1",
        "--results-dir",
        str(tmp_path),
    )

    assert main() == 1

    captured = capsys.readouterr()
    assert "unexpected internal error" in captured.out
    assert "internals" not in captured.out
    assert captured.err == ""
    assert list(tmp_path.glob("*.json")) == []


def test_eval_show_names_the_covered_task_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # Subset reports pin the full-suite lock_hash; the printed header names
    # the covered task ids so the report is self-describing.
    store = EvalReportStore(tmp_path, clock=FixedClock(datetime(2026, 9, 3, 12, 0, tzinfo=UTC)))
    report = BenchmarkReport(
        report_id="eval-subset",
        suite_version="1.0.0",
        lock_hash="0123456789abcdef" * 4,
        config_reports=(
            ConfigReport(
                config_id="baseline",
                task_id="bench-transient-api",
                trials=1,
                successes=1,
                false_successes=0,
                success_rate=1.0,
                false_success_rate=0.0,
                mean_cost_usd=0.02,
                mean_latency_seconds=1.5,
                mean_total_tokens=240.0,
                mean_human_interventions=0.0,
            ),
            ConfigReport(
                config_id="baseline",
                task_id="bench-provider-outage",
                trials=1,
                successes=0,
                false_successes=0,
                success_rate=0.0,
                false_success_rate=0.0,
                mean_cost_usd=0.0,
                mean_latency_seconds=0.25,
                mean_total_tokens=0.0,
                mean_human_interventions=0.0,
            ),
        ),
        pareto_config_ids=("baseline",),
    )
    store.save(report)
    _argv(monkeypatch, "eval", "--show", "eval-subset", "--results-dir", str(tmp_path))

    assert main() == 0

    out = capsys.readouterr().out
    assert "tasks covered (2): bench-provider-outage, bench-transient-api" in out


@_REQUIRES_GIT
@_REQUIRES_RLIMIT_AS
def test_eval_scripted_trial_runs_saves_lists_and_shows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _argv(
        monkeypatch,
        "eval",
        "--tasks",
        "bench-transient-api",
        "--trials",
        "1",
        "--config",
        "baseline",
        "--results-dir",
        str(tmp_path),
    )

    assert main() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert "eval report=eval-" in out
    assert "bench-transient-api" in out
    # A subset run pins the full-suite lock; the header self-describes coverage.
    assert "tasks covered (1): bench-transient-api" in out
    assert "100.0%" in out
    assert "pareto frontier: baseline" in out
    saved_line = next(line for line in out.splitlines() if line.startswith("report saved: "))
    report_path = Path(saved_line.removeprefix("report saved: "))
    assert report_path.is_file()
    report_id = report_path.stem

    _argv(monkeypatch, "eval", "--list", "--results-dir", str(tmp_path))

    assert main() == 0

    listed = capsys.readouterr().out
    assert report_id in listed
    assert "configs=baseline" in listed
    assert "tasks=1" in listed

    _argv(monkeypatch, "eval", "--show", report_id, "--results-dir", str(tmp_path))

    assert main() == 0

    shown = capsys.readouterr().out
    assert f"eval report={report_id}" in shown
    assert "bench-transient-api" in shown
    assert "pareto frontier: baseline" in shown


@pytest.mark.skipif(
    _rlimit_as_supported(),
    reason="trusted-sandbox platform preflight only fires where the host rejects RLIMIT_AS",
)
def test_eval_trusted_task_names_the_platform_cause_on_unsupported_hosts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # Regression: an unsupported host must fail fast with the true cause
    # (RLIMIT_AS rejected), never the downstream "scripted model exhausted"
    # ghost that sandbox-rejected commands used to surface.
    _argv(
        monkeypatch,
        "eval",
        "--tasks",
        "bench-transient-api",
        "--trials",
        "1",
        "--results-dir",
        str(tmp_path),
    )

    assert main() == 1

    captured = capsys.readouterr()
    assert "rejects setrlimit(RLIMIT_AS)" in captured.out
    assert "scripted model exhausted" not in captured.out
    assert list(tmp_path.glob("*.json")) == []
