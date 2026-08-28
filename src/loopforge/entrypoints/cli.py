from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from loopforge.adapters.context import BudgetedContextBuilder, CharsPerTokenCounter
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import ObservationContainsVerifier, ScriptedModel, ScriptedTools
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.events import ArtifactRecorded
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy
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
from loopforge.entrypoints.repair import (
    RepairRuntimeDeps,
    build_container_repair_runtime,
    build_trusted_repair_runtime,
)
from loopforge.ports.tools import ToolResult
from loopforge.workloads.fixtures import adder_repair_task


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


def _repair_demo(container_image: str | None) -> int:
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
    with tempfile.TemporaryDirectory(prefix="loopforge-repair-") as directory:
        store = InMemoryEventStore()
        telemetry = InMemoryTelemetry()
        deps = RepairRuntimeDeps(
            store=store, clock=SystemClock(), sleeper=SystemSleeper(), telemetry=telemetry
        )
        try:
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
        except ValueError as exc:
            print(f"error: invalid repair-demo configuration: {exc}")
            return 2
        try:
            state = bundle.runtime.run(task.objective)
        finally:
            bundle.close()
        events = store.events_for(state.run_id)
        artifacts = [event for event in events if isinstance(event, ArtifactRecorded)]
        print(
            f"run={state.run_id} status={state.status.value} "
            f"iterations={state.iteration} cost=${state.cost_usd:.2f}"
        )
        print(f"final verification: {state.last_verification}")
        print(
            f"workspace={bundle.workspace.workspace_id} "
            f"base_revision={bundle.workspace.base_revision}"
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="loopforge")
    parser.add_argument("command", choices=["demo", "repair-demo"])
    parser.add_argument(
        "--container",
        metavar="IMAGE",
        default=None,
        help="run repair-demo through the hardened container sandbox using IMAGE",
    )
    args = parser.parse_args()
    if args.command == "demo":
        return _demo()
    if args.command == "repair-demo":
        return _repair_demo(args.container)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
