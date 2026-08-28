"""Composition wiring for the software-repair reference workload.

Entrypoints own adapter wiring: the workload package stays port-facing, and
the runtime core stays workload-agnostic. Two honest execution paths exist:

- the *trusted development* path binds repair command tools with empty
  sandbox requirements against ``ConstrainedLocalSandbox`` — code-defined
  fixtures only, with the local adapter's honest capability report. Where the
  platform rejects the local launcher's resource limits, command execution
  fails closed (AGENTS.md rules 13-15) rather than degrading enforcement;
- the *untrusted* path binds the same tools with
  ``UNTRUSTED_REPAIR_REQUIREMENTS`` (process-filesystem and network
  isolation) against ``ContainerSandbox``, which fails closed anywhere the
  container backend is unavailable.

Shared wiring dependencies travel in ``RepairRuntimeDeps`` so the builders'
signatures stay small and explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loopforge.adapters.composite_tools import CompositeToolExecutor
from loopforge.adapters.container_sandbox import ContainerSandbox, ContainerSandboxConfig
from loopforge.adapters.context import BudgetedContextBuilder, CharsPerTokenCounter
from loopforge.adapters.file_tools import WorkspaceFileTools
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.local_sandbox import CommandSpec, ConstrainedLocalSandbox
from loopforge.adapters.sandbox_tools import SandboxCommandTools, SandboxToolBinding
from loopforge.adapters.scripted import ScriptedModel
from loopforge.adapters.workspace_git_tools import WorkspaceGitTools
from loopforge.application.runtime import Runtime
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.security import SandboxRequirements
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import BudgetLimit, Permission, RiskLevel
from loopforge.ports.clock import ClockPort, SleeperPort
from loopforge.ports.model import ModelPort
from loopforge.ports.sandbox import SandboxPort
from loopforge.ports.state_store import StateStorePort
from loopforge.ports.telemetry import TelemetryPort
from loopforge.ports.workspace import WorkspacePort
from loopforge.workloads.repair import (
    UNTRUSTED_REPAIR_REQUIREMENTS,
    RepairCommand,
    RepairContextBuilder,
    RepairTask,
    RepairVerifier,
    WorkspaceArtifactCollector,
    scripted_repair_actions,
)


def repair_command_specs(commands: tuple[RepairCommand, ...]) -> list[CommandSpec]:
    """Lower code-owned repair commands into sandbox command allowlist specs."""
    return [
        CommandSpec(
            name=command.name,
            argv=command.argv,
            timeout_seconds=command.timeout_seconds,
            cpu_seconds=command.cpu_seconds,
        )
        for command in commands
    ]


def repair_command_bindings(
    commands: tuple[RepairCommand, ...], *, requirements: SandboxRequirements
) -> list[SandboxToolBinding]:
    """Bind predefined repair commands as sandbox tools.

    The caller chooses the code-owned requirements contract: empty for the
    trusted development path, ``UNTRUSTED_REPAIR_REQUIREMENTS`` for untrusted
    fixture/build execution (which fails closed without container-grade
    isolation). Command execution runs repository build/test code, so tools
    are honestly classified as workspace-writing and non-retryable.
    """
    return [
        SandboxToolBinding(
            metadata=ToolMetadata(
                name=command.name,
                risk=RiskLevel.LOCAL_WRITE,
                required_permission=Permission.LOCAL_WRITE,
                side_effect=SideEffectClass.LOCAL_WRITE,
                retry=RetryClass.NEVER,
                idempotency=IdempotencyClass.NONE,
                approval=ApprovalClass.NONE,
                timeout_seconds=command.timeout_seconds,
            ),
            command_name=command.name,
            requirements=requirements,
        )
        for command in commands
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairRuntimeDeps:
    """Shared runtime dependencies for repair wiring."""

    store: StateStorePort
    clock: ClockPort
    sleeper: SleeperPort
    telemetry: TelemetryPort | None = None
    budget: BudgetLimit | None = None
    model: ModelPort | None = None
    """Optional live model; defaults to the deterministic scripted model."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairRuntimeBundle:
    runtime: Runtime
    workspace: WorkspacePort
    sandbox: SandboxPort

    def close(self) -> None:
        """Release sandbox resources (in-flight containers) best-effort.

        The composition root owns the sandbox lifecycle: callers should pair
        ``runtime.run(...)`` with ``close()`` in a ``finally`` block so an
        exceptional run never leaks an in-flight workload container.
        """
        destroy = getattr(self.sandbox, "destroy", None)
        if callable(destroy):
            destroy()
        close_model = getattr(self.runtime.model, "close", None)
        if callable(close_model):
            close_model()


def build_trusted_repair_runtime(
    task: RepairTask,
    *,
    workspaces_dir: str | Path,
    deps: RepairRuntimeDeps,
) -> RepairRuntimeBundle:
    """Wire the deterministic repair stack on the trusted local sandbox.

    This path executes code-defined fixtures through the full runtime with a
    scripted model; it does not claim hostile-code isolation. Untrusted
    fixture execution must use :func:`build_container_repair_runtime`.
    """
    workspace = _materialize_workspace(task, workspaces_dir=workspaces_dir)
    sandbox: SandboxPort = ConstrainedLocalSandbox(
        workspace.root,
        commands=repair_command_specs(task.commands),
        environment={},
    )
    return _repair_bundle(
        task,
        workspace=workspace,
        sandbox=sandbox,
        requirements=SandboxRequirements(),
        deps=deps,
    )


def build_container_repair_runtime(
    task: RepairTask,
    *,
    image: str,
    workspaces_dir: str | Path,
    deps: RepairRuntimeDeps,
) -> RepairRuntimeBundle:
    """Wire the repair stack for untrusted fixture execution (PACS-009 boundary).

    Command tools declare ``UNTRUSTED_REPAIR_REQUIREMENTS``, so binding fails
    closed anywhere container-grade isolation is unavailable. The task's
    command argv must address the in-container interpreter (for example
    ``/usr/local/bin/python``); image trust is operator-owned.
    """
    workspace = _materialize_workspace(task, workspaces_dir=workspaces_dir)
    sandbox: SandboxPort = ContainerSandbox(
        workspace.root,
        config=ContainerSandboxConfig(
            image=image,
            commands=tuple(repair_command_specs(task.commands)),
            environment={},
        ),
    )
    return _repair_bundle(
        task,
        workspace=workspace,
        sandbox=sandbox,
        requirements=UNTRUSTED_REPAIR_REQUIREMENTS,
        deps=deps,
    )


def _materialize_workspace(task: RepairTask, *, workspaces_dir: str | Path) -> WorkspacePort:
    manager = GitWorkspaceManager(workspaces_dir)
    return manager.materialize(task.fixture)


def _repair_bundle(
    task: RepairTask,
    *,
    workspace: WorkspacePort,
    sandbox: SandboxPort,
    requirements: SandboxRequirements,
    deps: RepairRuntimeDeps,
) -> RepairRuntimeBundle:
    tools = CompositeToolExecutor(
        (
            WorkspaceFileTools(sandbox, workspace.root),
            WorkspaceGitTools(workspace),
            SandboxCommandTools(
                sandbox,
                repair_command_bindings(task.commands, requirements=requirements),
            ),
        )
    )
    runtime = Runtime(
        model=(
            deps.model if deps.model is not None else ScriptedModel(scripted_repair_actions(task))
        ),
        tools=tools,
        verifier=RepairVerifier(sandbox, workspace, task.acceptance),
        store=deps.store,
        control=ControlPolicy(deps.budget or BudgetLimit(max_cost_usd=1.0, max_iterations=8)),
        permissions=PermissionPolicy(frozenset({Permission.READ, Permission.LOCAL_WRITE})),
        reliability=ReliabilityPolicy(),
        context=RepairContextBuilder(
            BudgetedContextBuilder(
                deps.clock,
                CharsPerTokenCounter(),
                template=default_controller_template(),
                token_budget=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
            )
        ),
        clock=deps.clock,
        sleeper=deps.sleeper,
        telemetry=deps.telemetry,
        artifacts=WorkspaceArtifactCollector(workspace),
    )
    return RepairRuntimeBundle(runtime=runtime, workspace=workspace, sandbox=sandbox)
