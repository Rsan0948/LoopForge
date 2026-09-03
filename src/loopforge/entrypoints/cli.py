from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from loopforge.adapters.context import BudgetedContextBuilder, CharsPerTokenCounter
from loopforge.adapters.deepseek_model import DeepSeekModel
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.scripted import ObservationContainsVerifier, ScriptedModel, ScriptedTools
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.eval_runner import run_trials
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.benchmarks import (
    BenchmarkReport,
    BenchmarkSuite,
    BenchmarkTaskSpec,
)
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.events import ArtifactRecorded, RunStopped
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.routing import ModelCapabilities, ModelTier
from loopforge.domain.telemetry import LogRecord, MetricSample, Span, TelemetryRecord
from loopforge.domain.tooling import (
    ApprovalClass,
    DataSensitivity,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, BudgetLimit, Permission, RiskLevel, RunStatus
from loopforge.domain.workspace import (
    AcceptanceCriteria,
    FixtureFile,
    FixtureSpec,
    PatchConstraints,
)
from loopforge.entrypoints.eval import (
    EvalReportStore,
    EvalReportStoreError,
    EvalTrialDriver,
    EvalTrialError,
    resolve_configurations,
)
from loopforge.entrypoints.orchestrated import build_orchestrated_repair_runtime
from loopforge.entrypoints.profile import LoopProfile, ProfileError, load_profile
from loopforge.entrypoints.repair import (
    RepairRuntimeDeps,
    build_adopted_repair_runtime,
    build_container_repair_runtime,
    build_trusted_repair_runtime,
)
from loopforge.ports.model import ModelPort
from loopforge.ports.tools import ToolResult
from loopforge.workloads.benchmarks import (
    benchmark_suite,
    build_benchmark_binding,
)
from loopforge.workloads.fixtures import adder_repair_task, calculator_repair_task
from loopforge.workloads.repair import (
    OrchestratedRepairTask,
    RepairCommand,
    RepairCommandKind,
    RepairTask,
    repair_tool_specs,
)

_OLLAMA_API_KEY_ENV = "LOOPFORGE_OLLAMA_API_KEY"
_DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"


def _metadata(
    name: str,
    *,
    risk: RiskLevel,
    side_effect: SideEffectClass,
    sensitivity: DataSensitivity = DataSensitivity.INTERNAL,
) -> ToolMetadata:
    required_permission = {
        RiskLevel.READ_ONLY: Permission.READ,
        RiskLevel.LOCAL_WRITE: Permission.LOCAL_WRITE,
        RiskLevel.EXTERNAL_WRITE: Permission.EXTERNAL_WRITE,
        RiskLevel.CRITICAL: Permission.CRITICAL,
    }[risk]
    return ToolMetadata(
        name=name,
        risk=risk,
        required_permission=required_permission,
        side_effect=side_effect,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
        sensitivity=sensitivity,
    )


def _format_attributes(record: Span | LogRecord | MetricSample) -> str:
    if not record.attributes:
        return "-"
    return " ".join(f"{key}={value}" for key, value in record.attributes.items())


def format_telemetry_narrative(records: tuple[TelemetryRecord, ...]) -> str:
    """Render the recorded telemetry as a causally correlated trace/log narrative.

    The narrative is a human-readable view of the non-authoritative telemetry
    projection: spans nest under the run root by parent id, and logs/metrics
    carry the same run/cycle/action/verification correlation identifiers.
    """
    lines = ["telemetry narrative (non-authoritative projection; event store is authoritative):"]
    lines.append("trace:")
    lines.extend(
        f"  span {record.name} id={record.span_id} "
        f"parent={record.parent_span_id or '-'} status={record.status.value} "
        f"cycle={record.correlation.cycle or '-'} "
        f"action={record.correlation.action_id or '-'} "
        f"attrs={_format_attributes(record)}"
        for record in records
        if isinstance(record, Span)
    )
    lines.append("logs:")
    lines.extend(
        f"  {record.severity.value} {record.message} "
        f"run={record.correlation.run_id} cycle={record.correlation.cycle or '-'} "
        f"action={record.correlation.action_id or '-'} "
        f"verification={record.correlation.verification_id or '-'} "
        f"attrs={_format_attributes(record)}"
        for record in records
        if isinstance(record, LogRecord)
    )
    lines.append("metrics:")
    lines.extend(
        f"  {record.name} {record.kind.value}={record.value} "
        f"cycle={record.correlation.cycle or '-'}"
        for record in records
        if isinstance(record, MetricSample)
    )
    return "\n".join(lines)


def _demo() -> int:
    telemetry = InMemoryTelemetry()
    runtime = Runtime(
        model=ScriptedModel(
            [
                ActionProposal(ActionId("a1"), "inspect", {"target": "auth"}),
                ActionProposal(ActionId("a2"), "fix", {"target": "auth"}),
            ]
        ),
        tools=ScriptedTools(
            [
                ToolResult(ok=True, observation="tests still failing"),
                ToolResult(ok=True, observation="all tests pass"),
            ],
            metadata=[
                _metadata(
                    "inspect",
                    risk=RiskLevel.READ_ONLY,
                    side_effect=SideEffectClass.READ_ONLY,
                ),
                # The fix tool handles sensitive repository content: its
                # observation must be redacted in telemetry while remaining
                # intact in the authoritative event store.
                _metadata(
                    "fix",
                    risk=RiskLevel.LOCAL_WRITE,
                    side_effect=SideEffectClass.LOCAL_WRITE,
                    sensitivity=DataSensitivity.SENSITIVE,
                ),
            ],
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=InMemoryEventStore(),
        control=ControlPolicy(BudgetLimit(max_cost_usd=1.0, max_iterations=5)),
        permissions=PermissionPolicy(frozenset({Permission.READ, Permission.LOCAL_WRITE})),
        reliability=ReliabilityPolicy(),
        context=BudgetedContextBuilder(
            SystemClock(),
            CharsPerTokenCounter(),
            template=default_controller_template(),
            token_budget=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
        ),
        clock=SystemClock(),
        sleeper=SystemSleeper(),
        telemetry=telemetry,
    )
    state = runtime.run("Repair authentication regression")
    print(
        f"run={state.run_id} status={state.status.value} "
        f"iterations={state.iteration} cost=${state.cost_usd:.2f}"
    )
    print(format_telemetry_narrative(telemetry.records))
    return 0


def build_ollama_model(
    task: RepairTask,
    *,
    model_name: str,
    base_url: str,
    context_window_tokens: int = 131_072,
) -> OllamaModel:
    """Wire the live Ollama adapter; credentials come from the environment only.

    Honest per-model capability metadata is supplied here at wiring time (the
    PACS-012 registry seam): the operator-owned context window for the
    deployed model replaces the adapter's deliberately conservative default.
    """
    return OllamaModel(
        model=model_name,
        tools=repair_tool_specs(task),
        template=default_controller_template(),
        base_url=base_url,
        api_key=(os.environ.get(_OLLAMA_API_KEY_ENV) or "").strip() or None,
        capabilities=ModelCapabilities(
            provider="ollama",
            model=model_name,
            supports_tool_calls=True,
            context_window_tokens=context_window_tokens,
        ),
    )


def build_deepseek_model(task: RepairTask, *, model_name: str) -> DeepSeekModel:
    """Wire DeepSeek using the environment as the only credential source."""
    api_key = (os.environ.get(_DEEPSEEK_API_KEY_ENV) or "").strip()
    if not api_key:
        msg = f"{_DEEPSEEK_API_KEY_ENV} is required for --model deepseek"
        raise ValueError(msg)
    return DeepSeekModel(
        api_key=api_key,
        model=model_name,
        tools=repair_tool_specs(task),
        template=default_controller_template(),
    )


def _close_model_quietly(model: ModelPort | None) -> None:
    close = getattr(model, "close", None)
    if callable(close):
        close()


def _repair_demo(  # noqa: PLR0912, PLR0913 - CLI wiring keeps provider options explicit
    container_image: str | None,
    *,
    model_kind: str = "scripted",
    ollama_model: str = "devstral-small-2:latest",
    ollama_url: str = "http://localhost:11434",
    ollama_context_window: int = 131_072,
    deepseek_model: str = "deepseek-v4-pro",
) -> int:
    """Repair a fixture repository through the full runtime, deterministically.

    Default is the trusted-development path: a code-defined fixture on the
    local sandbox with a scripted model. ``--container IMAGE`` selects the
    untrusted path explicitly: the same fixture executes through the hardened
    container sandbox with code-owned isolation requirements. Success is
    granted only by the independent verifier stack, and the exact patch plus
    verification evidence are captured as durable, replayable events.
    """
    if container_image is not None and not container_image.strip():
        print("error: --container requires a non-empty image reference")
        return 2
    if container_image is not None:
        task = adder_repair_task(executable="/usr/local/bin/python")
    else:
        task = adder_repair_task()
    model: ModelPort | None = None
    bundle_owned = False
    try:
        if model_kind == "ollama":
            model = build_ollama_model(
                task,
                model_name=ollama_model,
                base_url=ollama_url,
                context_window_tokens=ollama_context_window,
            )
        elif model_kind == "deepseek":
            model = build_deepseek_model(task, model_name=deepseek_model)
        with tempfile.TemporaryDirectory(prefix="loopforge-repair-") as directory:
            store = InMemoryEventStore()
            telemetry = InMemoryTelemetry()
            deps = RepairRuntimeDeps(
                store=store,
                clock=SystemClock(),
                sleeper=SystemSleeper(),
                telemetry=telemetry,
                model=model,
                model_tier=(
                    ModelTier.ADVANCED
                    if model_kind == "deepseek"
                    else ModelTier.STANDARD
                    if model_kind == "ollama"
                    else ModelTier.ECONOMY
                ),
            )
            if container_image is not None:
                bundle = build_container_repair_runtime(
                    task,
                    image=container_image,
                    workspaces_dir=Path(directory),
                    deps=deps,
                )
            else:
                bundle = build_trusted_repair_runtime(
                    task,
                    workspaces_dir=Path(directory),
                    deps=deps,
                )
            # The bundle now owns the model lifecycle (bundle.close()).
            bundle_owned = True
            try:
                state = bundle.runtime.run(task.objective)
            finally:
                bundle.close()
    except ValueError as exc:
        print(f"error: invalid repair-demo configuration: {exc}")
        return 2
    finally:
        if not bundle_owned:
            # Construction succeeded but bundle wiring never took ownership;
            # never leak the adapter's HTTP client on any failure path.
            _close_model_quietly(model)
    events = store.events_for(state.run_id)
    artifacts = [event for event in events if isinstance(event, ArtifactRecorded)]
    stop_event = next((event for event in reversed(events) if isinstance(event, RunStopped)), None)
    print(
        f"run={state.run_id} status={state.status.value} "
        f"iterations={state.iteration} cost=${state.cost_usd:.2f}"
    )
    if state.stop_reason is not None:
        print(f"stop_reason={state.stop_reason.value}")
    if stop_event is not None:
        print(f"stop_summary={stop_event.summary}")
    print(f"final verification: {state.last_verification}")
    print(
        f"workspace={bundle.workspace.workspace_id} base_revision={bundle.workspace.base_revision}"
    )
    print(f"evidence artifacts recorded: {len(artifacts)}")
    if artifacts:
        latest = artifacts[-1]
        print(
            f"latest artifact: kind={latest.kind.value} label={latest.label} "
            f"bytes={len(latest.content.encode('utf-8'))}"
        )
        print("exact patch evidence:")
        print(latest.content)
    return 0 if state.status is RunStatus.SUCCEEDED else 1


def _orchestrated_repair_demo(container_image: str | None) -> int:
    """Repair the two-module calculator fixture with two isolated workers.

    The benchmarkable multi-agent path (PACS-013): one worker repairs
    ``adder.py``, another repairs ``greeter.py``, each in its own linked
    worktree with a static budget share; the orchestrator merges their
    verified patches in spawn order and the integration verifier grants
    success only when the full merged suite passes. The single-runtime
    ``repair-demo`` remains the untouched default reference behavior.
    """
    if container_image is not None and not container_image.strip():
        print("error: --container requires a non-empty image reference")
        return 2
    executable = "/usr/local/bin/python" if container_image is not None else None
    task = calculator_repair_task(executable=executable)
    try:
        with tempfile.TemporaryDirectory(prefix="loopforge-orchestrated-") as directory:
            store = InMemoryEventStore()
            deps = RepairRuntimeDeps(
                store=store,
                clock=SystemClock(),
                sleeper=SystemSleeper(),
                telemetry=InMemoryTelemetry(),
            )
            bundle = build_orchestrated_repair_runtime(
                task,
                workspaces_dir=Path(directory),
                deps=deps,
                container_image=container_image,
            )
            try:
                state = bundle.orchestrator.run(task.objective, task.plan)
            finally:
                bundle.close()
    except ValueError as exc:
        print(f"error: invalid orchestrated-repair-demo configuration: {exc}")
        return 2
    events = store.events_for(state.run_id)
    artifacts = [event for event in events if isinstance(event, ArtifactRecorded)]
    stop_event = next((event for event in reversed(events) if isinstance(event, RunStopped)), None)
    print(
        f"run={state.run_id} status={state.status.value} "
        f"iterations={state.iteration} cost=${state.cost_usd:.2f}"
    )
    if state.stop_reason is not None:
        print(f"stop_reason={state.stop_reason.value}")
    if stop_event is not None:
        print(f"stop_summary={stop_event.summary}")
    for worker in state.workers:
        outcome = worker.outcome.value if worker.outcome is not None else "-"
        merge = worker.merge_outcome.value if worker.merge_outcome is not None else "-"
        print(
            f"worker={worker.worker_id} run={worker.worker_run_id} outcome={outcome} "
            f"merge={merge} budget_share=${worker.budget_share_cost_usd:.2f}"
        )
    print(f"final verification: {state.last_verification}")
    print(f"evidence artifacts recorded: {len(artifacts)}")
    return 0 if state.status is RunStatus.SUCCEEDED else 1


def _civicml_loop(repository: str, *, deepseek_model: str, container_image: str) -> int:
    """Run DeepSeek repair iterations against an adopted CivicML checkout."""
    root = Path(repository).resolve()
    python = root / ".venv/bin/python"
    if not container_image and not python.is_file():
        print(f"error: expected CivicML virtualenv interpreter at {python}")
        return 2
    task = RepairTask(
        task_id="civicml-loop",
        objective=(
            "Fix every currently failing CivicML test and keep the existing documented behavior. "
            "Use the real source and tests in this checkout. Iterate: inspect failures, make the "
            "smallest correct edits, run the checks, and continue until all checks pass. "
            "Do not stop to summarize while checks fail: every turn must use a tool. Begin by "
            "reading the failing test and its implementation, then edit the implementation or "
            "test only when the evidence supports it."
        ),
        fixture=FixtureSpec(
            fixture_id="adopted-civicml",
            files=(FixtureFile(path="pyproject.toml", content="adopted checkout"),),
        ),
        commands=(
            RepairCommand(
                kind=RepairCommandKind.TEST,
                name="civicml_tests",
                argv=(
                    "/usr/local/bin/python" if container_image else str(python),
                    "-m",
                    "pytest",
                    "-q",
                    "--ignore=tests/research",
                ),
                timeout_seconds=300,
                cpu_seconds=240,
            ),
            RepairCommand(
                kind=RepairCommandKind.LINT,
                name="civicml_ruff",
                argv=(
                    "/usr/local/bin/ruff" if container_image else str(python),
                    "check",
                    "apps",
                    "packages",
                    "tests",
                ),
                timeout_seconds=120,
                cpu_seconds=120,
            ),
        ),
        acceptance=AcceptanceCriteria(
            required_commands=("civicml_tests", "civicml_ruff"),
            patch=PatchConstraints(
                require_change=True,
                allowed_prefixes=(
                    "apps",
                    "packages",
                    "tests",
                    "pyproject.toml",
                    "requirements.txt",
                ),
                max_changed_files=30,
            ),
        ),
    )
    model = build_deepseek_model(task, model_name=deepseek_model)
    store = InMemoryEventStore()
    deps = RepairRuntimeDeps(
        store=store,
        clock=SystemClock(),
        sleeper=SystemSleeper(),
        telemetry=InMemoryTelemetry(),
        model=model,
        model_tier=ModelTier.ADVANCED,
        budget=BudgetLimit(max_cost_usd=5.0, max_iterations=30),
    )
    bundle = build_adopted_repair_runtime(
        task,
        repository=root,
        deps=deps,
        container_image=container_image or None,
        environment={"CIVICML_ENV": "test"},
    )
    try:
        state = bundle.runtime.run(task.objective)
    finally:
        bundle.close()
    print(
        f"run={state.run_id} status={state.status.value} "
        f"iterations={state.iteration} cost=${state.cost_usd:.2f}"
    )
    print(f"verification={state.last_verification}")
    return 0 if state.status is RunStatus.SUCCEEDED else 1


def _print_profile_summary(profile: LoopProfile) -> None:
    """Render the resolved profile for --dry-run review (no model, no sandbox)."""
    task = profile.task
    mode = f"container image={profile.container_image}" if profile.container_image else "local"
    print(f"profile task={task.task_id} repository={profile.repository} mode={mode}")
    print(f"objective={task.objective}")
    for command in task.commands:
        print(
            f"check {command.name} kind={command.kind.value} "
            f"timeout={command.timeout_seconds:g}s cpu={command.cpu_seconds}s "
            f"argv={' '.join(command.argv)}"
        )
    patch = task.acceptance.patch
    print(
        f"acceptance required={list(task.acceptance.required_commands)} "
        f"require_change={patch.require_change} "
        f"allowed_prefixes={list(patch.allowed_prefixes)} "
        f"max_changed_files={patch.max_changed_files}"
    )
    print(f"environment keys={sorted(profile.environment)}")
    print(
        f"model provider={profile.model_provider} name={profile.model_name} "
        f"tier={profile.model_tier.value}"
    )
    budget = profile.budget
    print(
        f"budget max_cost=${budget.max_cost_usd:.2f} "
        f"max_iterations={budget.max_iterations} "
        f"max_total_tokens={budget.max_total_tokens or '-'} "
        f"max_elapsed_seconds={budget.max_elapsed_seconds or '-'}"
    )


def _profile_loop(
    profile_path: str,
    *,
    dry_run: bool,
    ollama_url: str,
    ollama_context_window: int,
) -> int:
    """Run repair iterations against any local checkout described by a profile.

    The profile (operator-owned, loaded from outside the target repository)
    supplies the task, checks, acceptance contract, sandbox environment,
    model selection, and budget that ``civicml-loop`` hardcodes.
    """
    try:
        profile = load_profile(profile_path)
    except ProfileError as exc:
        print(f"error: invalid loop profile: {exc}")
        return 2
    if dry_run:
        _print_profile_summary(profile)
        return 0
    task = profile.task
    model: ModelPort | None = None
    bundle_owned = False
    try:
        if profile.model_provider == "deepseek":
            model = build_deepseek_model(task, model_name=profile.model_name)
        elif profile.model_provider == "ollama":
            model = build_ollama_model(
                task,
                model_name=profile.model_name,
                base_url=ollama_url,
                context_window_tokens=ollama_context_window,
            )
        store = InMemoryEventStore()
        deps = RepairRuntimeDeps(
            store=store,
            clock=SystemClock(),
            sleeper=SystemSleeper(),
            telemetry=InMemoryTelemetry(),
            model=model,
            model_tier=profile.model_tier,
            budget=profile.budget,
            no_progress_limit=profile.no_progress_limit,
        )
        bundle = build_adopted_repair_runtime(
            task,
            repository=profile.repository,
            deps=deps,
            container_image=profile.container_image,
            environment=profile.environment,
            limits=profile.limits,
        )
        bundle_owned = True
        try:
            state = bundle.runtime.run(task.objective)
        finally:
            bundle.close()
    except ValueError as exc:
        print(f"error: invalid loop configuration: {exc}")
        return 2
    finally:
        if not bundle_owned:
            _close_model_quietly(model)
    print(
        f"run={state.run_id} status={state.status.value} "
        f"iterations={state.iteration} cost=${state.cost_usd:.2f}"
    )
    print(f"verification={state.last_verification}")
    return 0 if state.status is RunStatus.SUCCEEDED else 1


def _eval_specs(tasks_arg: str) -> tuple[tuple[BenchmarkTaskSpec, ...], BenchmarkSuite]:
    """Resolve --tasks against the locked suite; unknown ids fail closed."""
    suite = benchmark_suite()
    if tasks_arg.strip().lower() == "all":
        return suite.tasks, suite
    names = [name.strip() for name in tasks_arg.split(",") if name.strip()]
    if not names:
        msg = "--tasks must be 'all' or a comma-separated list of benchmark task ids"
        raise ValueError(msg)
    if len(set(names)) != len(names):
        msg_2 = "--tasks entries must be unique"
        raise ValueError(msg_2)
    return tuple(build_benchmark_binding(name).spec for name in names), suite


def _eval_model_factory(
    args: argparse.Namespace,
) -> Callable[[RepairTask | OrchestratedRepairTask], ModelPort]:
    """Build the live Ollama adapter per trial; credentials from the environment only.

    For the orchestrated binding the tool catalog comes from the first worker
    assignment: the repair tool set is identical across workers, and the
    builder registers the shared adapter per worker exactly like the
    orchestrated entrypoint precedent.
    """

    def factory(task: RepairTask | OrchestratedRepairTask) -> ModelPort:
        spec_task = task if isinstance(task, RepairTask) else task.assignments[0].task
        return build_ollama_model(
            spec_task,
            model_name=args.ollama_model,
            base_url=args.ollama_url,
            context_window_tokens=args.ollama_context_window,
        )

    return factory


def _print_eval_report(report: BenchmarkReport, *, saved_path: Path | None = None) -> None:
    # Subset reports stay self-describing: --tasks <subset> still pins the
    # full-suite lock_hash, so the header names the covered task ids.
    task_ids = sorted({entry.task_id for entry in report.config_reports})
    print(
        f"eval report={report.report_id} suite={report.suite_version} lock={report.lock_hash[:12]}"
    )
    print(f"tasks covered ({len(task_ids)}): {', '.join(task_ids)}")
    print(
        f"{'config':<14} {'task':<26} {'trials':>6} {'success':>8} "
        f"{'false-succ':>10} {'cost':>9} {'latency':>9} {'interv':>7}"
    )
    for entry in report.config_reports:
        print(
            f"{entry.config_id:<14} {entry.task_id:<26} {entry.trials:>6} "
            f"{entry.success_rate:>8.1%} {entry.false_success_rate:>10.1%} "
            f"${entry.mean_cost_usd:>8.4f} {entry.mean_latency_seconds:>8.2f}s "
            f"{entry.mean_human_interventions:>7.2f}"
        )
    print(f"pareto frontier: {', '.join(report.pareto_config_ids)}")
    if saved_path is not None:
        print(f"report saved: {saved_path}")


def _eval(args: argparse.Namespace) -> int:  # noqa: PLR0911, PLR0912 - CLI flow keeps one return per outcome
    """Run the locked benchmark suite through the M5 multi-trial eval runner.

    Default is fully deterministic: scripted model, no credentials, no
    network. ``--model ollama`` is the operator's explicit live-trial choice
    (and defaults the container image for CONTAINER-mode tasks). Reports are
    operator-owned JSON artifacts under --results-dir.
    """
    store = EvalReportStore(Path(args.results_dir))
    if args.list:
        try:
            summaries = store.list()
        except EvalReportStoreError as exc:
            print(f"error: {exc}")
            return 1
        if not summaries:
            print(f"no eval reports in {args.results_dir}")
            return 0
        for summary in summaries:
            print(
                f"{summary.report_id} suite={summary.suite_version} "
                f"lock={summary.lock_hash[:12]} created={summary.created_at} "
                f"configs={','.join(summary.config_ids)} tasks={len(summary.task_ids)}"
            )
        return 0
    if args.show is not None:
        try:
            report = store.load(args.show)
        except EvalReportStoreError as exc:
            print(f"error: {exc}")
            return 1
        _print_eval_report(report)
        return 0
    if args.model not in {"scripted", "ollama"}:
        print(f"error: eval supports --model scripted|ollama, not {args.model!r}")
        return 2
    if args.trials < 1:
        print("error: --trials must be at least 1")
        return 2
    try:
        specs, suite = _eval_specs(args.tasks)
        configs = resolve_configurations(tuple(args.config or ["baseline"]))
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    container = args.container
    if args.model == "ollama" and container is None:
        # Live trials of CONTAINER-mode tasks need the hardened sandbox; the
        # default image is the operator-overridable house reference.
        container = "python:3.12-alpine"
    model_factory = None if args.model == "scripted" else _eval_model_factory(args)
    report_id = f"eval-{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}-{suite.lock_hash[:8]}"
    with tempfile.TemporaryDirectory(prefix="loopforge-eval-") as directory:
        driver = EvalTrialDriver(
            workspaces_root=Path(directory),
            model_factory=model_factory,
            model_tier=ModelTier.ECONOMY if args.model == "scripted" else ModelTier.STANDARD,
            container_image=container,
        )
        try:
            report = run_trials(
                specs,
                configs,
                args.trials,
                driver,
                report_id=report_id,
                suite_version=suite.version,
                lock_hash=suite.lock_hash,
            )
        except EvalTrialError as exc:
            print(f"error: eval trial failed: {exc}")
            return 1
        except Exception:
            # CLI-leak precedent (PACS-011): an unexpected driver/runner
            # failure is a clean exit-1 line with no internals — never a raw
            # traceback leaking wiring detail or paths to the terminal.
            print("error: eval aborted with an unexpected internal error; no report written")
            return 1
    try:
        saved_path = store.save(report)
    except EvalReportStoreError as exc:
        print(f"error: {exc}")
        return 1
    _print_eval_report(report, saved_path=saved_path)
    return 0


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _warn_unless_loopback(host: str, port: int) -> None:
    """Loudly flag off-loopback binds on a deliberately unauthenticated server."""
    if host not in _LOOPBACK_HOSTS:
        print(
            f"warning: serve is binding {host}:{port} with NO authentication; "
            "every reachable host can drive runs, execute sandboxed checks, and "
            "roll back adopted checkouts. Bind 127.0.0.1 unless you know why.",
            file=sys.stderr,
        )


def _serve(  # noqa: PLR0913 - CLI wiring keeps server options explicit
    *,
    host: str,
    port: int,
    dsn: str,
    sqlite: str | None,
    data_dir: str,
    static_dir: str | None,
    evals_dir: str | None,
) -> int:
    """Run the operator server (PACS-014): REST + WebSocket over the durable store.

    Trusted-operator local tool (D10): no authentication; the default bind is
    loopback only. Postgres is the default store; ``--sqlite`` switches to a
    local SQLite file (the same codec and store semantics).
    """
    # Lazy imports: sessions.py reuses this module's model builders, so a
    # top-level server import here would close an import cycle.
    import uvicorn  # noqa: PLC0415

    from loopforge.entrypoints.server import ServerSettings, create_app  # noqa: PLC0415

    _warn_unless_loopback(host, port)

    settings = ServerSettings(
        store_kind="sqlite" if sqlite is not None else "postgres",
        dsn=dsn,
        sqlite_path=sqlite if sqlite is not None else ".loopforge/server/events.db",
        data_dir=Path(data_dir),
        static_dir=Path(static_dir) if static_dir else None,
        evals_dir=Path(evals_dir) if evals_dir else None,
    )
    uvicorn.run(create_app(settings), host=host, port=port)
    return 0


def main() -> int:  # noqa: PLR0911 - CLI dispatch keeps one return per command
    parser = argparse.ArgumentParser(prog="loopforge", epilog="legacy commands: {demo,repair-demo}")
    parser.add_argument(
        "command",
        choices=[
            "demo",
            "repair-demo",
            "orchestrated-repair-demo",
            "civicml-loop",
            "loop",
            "serve",
            "eval",
        ],
    )
    parser.add_argument(
        "profile",
        nargs="?",
        default=None,
        help="path to an operator-owned loop profile TOML (required by the loop command)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="loop only: print the resolved profile without running the model or sandbox",
    )
    parser.add_argument("--repository", default="/Users/rubensanchez/Developer/civicml-loopforge")
    parser.add_argument("--container-image", default="civicml-loopforge:integration")
    parser.add_argument(
        "--container",
        metavar="IMAGE",
        default=None,
        help="run repair-demo through the hardened container sandbox using IMAGE",
    )
    parser.add_argument(
        "--model",
        choices=["scripted", "ollama", "deepseek"],
        default="scripted",
        help="model backend for repair-demo (default: deterministic scripted model)",
    )
    parser.add_argument(
        "--deepseek-model",
        metavar="NAME",
        default="deepseek-v4-pro",
        help="DeepSeek model used with --model deepseek",
    )
    parser.add_argument(
        "--ollama-model",
        metavar="NAME",
        default="devstral-small-2:latest",
        help="Ollama model tag used with --model ollama",
    )
    parser.add_argument(
        "--ollama-url",
        metavar="URL",
        default="http://localhost:11434",
        help="Ollama server base URL used with --model ollama",
    )
    parser.add_argument(
        "--ollama-context-window",
        metavar="TOKENS",
        type=int,
        default=131_072,
        help="honest context window of the deployed Ollama model, registered as "
        "routing capability metadata (default: 131072)",
    )
    parser.add_argument(
        "--tasks",
        metavar="LIST",
        default="all",
        help="eval only: 'all' or a comma-separated list of benchmark task ids (default: all)",
    )
    parser.add_argument(
        "--trials",
        metavar="N",
        type=int,
        default=3,
        help="eval only: trials per (config, task) pair (default: 3)",
    )
    parser.add_argument(
        "--config",
        metavar="NAME",
        action="append",
        default=None,
        help="eval only: configuration preset (repeatable; default: baseline)",
    )
    parser.add_argument(
        "--results-dir",
        metavar="DIR",
        default=".loopforge/evals",
        help="eval only: operator-owned eval report store directory",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="eval only: list stored eval reports in --results-dir and exit",
    )
    parser.add_argument(
        "--show",
        metavar="REPORT_ID",
        default=None,
        help="eval only: reprint the stored eval report REPORT_ID and exit",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="serve only: bind host (default: 127.0.0.1; trusted-operator local tool)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8123,
        help="serve only: bind port (default: 8123)",
    )
    parser.add_argument(
        "--dsn",
        metavar="DSN",
        default="postgresql://loopforge:loopforge@127.0.0.1:5432/loopforge",
        help="serve only: Postgres event-store DSN (default store)",
    )
    parser.add_argument(
        "--sqlite",
        metavar="PATH",
        default=None,
        help="serve only: use a SQLite event store at PATH instead of Postgres",
    )
    parser.add_argument(
        "--data-dir",
        metavar="DIR",
        default=".loopforge/server",
        help="serve only: server-owned state dir (session registry, inline profiles)",
    )
    parser.add_argument(
        "--static-dir",
        metavar="DIR",
        default=None,
        help="serve only: built UI directory (for example ui/dist) mounted at /",
    )
    parser.add_argument(
        "--evals-dir",
        metavar="DIR",
        default=None,
        help="serve only: operator-owned eval report store the /api/evals routes read "
        "(default: DATA-DIR/evals)",
    )
    args = parser.parse_args()
    if args.profile is not None and args.command != "loop":
        parser.error(f"unrecognized arguments: {args.profile}")
    if args.command == "demo":
        return _demo()
    if args.command == "repair-demo":
        return _repair_demo(
            args.container,
            model_kind=args.model,
            ollama_model=args.ollama_model,
            ollama_url=args.ollama_url,
            ollama_context_window=args.ollama_context_window,
            deepseek_model=args.deepseek_model,
        )
    if args.command == "orchestrated-repair-demo":
        return _orchestrated_repair_demo(args.container)
    if args.command == "civicml-loop":
        return _civicml_loop(
            args.repository,
            deepseek_model=args.deepseek_model,
            container_image=args.container_image,
        )
    if args.command == "loop":
        if not args.profile:
            print("error: the loop command requires a profile TOML path")
            return 2
        return _profile_loop(
            args.profile,
            dry_run=args.dry_run,
            ollama_url=args.ollama_url,
            ollama_context_window=args.ollama_context_window,
        )
    if args.command == "eval":
        return _eval(args)
    if args.command == "serve":
        return _serve(
            host=args.host,
            port=args.port,
            dsn=args.dsn,
            sqlite=args.sqlite,
            data_dir=args.data_dir,
            static_dir=args.static_dir,
            evals_dir=args.evals_dir,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
