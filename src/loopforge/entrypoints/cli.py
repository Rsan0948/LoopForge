from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from loopforge.adapters.context import BudgetedContextBuilder, CharsPerTokenCounter
from loopforge.adapters.deepseek_model import DeepSeekModel
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.scripted import ObservationContainsVerifier, ScriptedModel, ScriptedTools
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
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
from loopforge.entrypoints.orchestrated import build_orchestrated_repair_runtime
from loopforge.entrypoints.repair import (
    RepairRuntimeDeps,
    build_adopted_repair_runtime,
    build_container_repair_runtime,
    build_trusted_repair_runtime,
)
from loopforge.ports.model import ModelPort
from loopforge.ports.tools import ToolResult
from loopforge.workloads.fixtures import adder_repair_task, calculator_repair_task
from loopforge.workloads.repair import (
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
        task, repository=root, deps=deps, container_image=container_image or None
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="loopforge", epilog="legacy commands: {demo,repair-demo}")
    parser.add_argument(
        "command", choices=["demo", "repair-demo", "orchestrated-repair-demo", "civicml-loop"]
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
    args = parser.parse_args()
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
