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

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from loopforge.adapters.approval_gate_tools import ApprovalGateTools
from loopforge.adapters.composite_tools import CompositeToolExecutor, NamedToolExecutor
from loopforge.adapters.container_sandbox import ContainerSandbox, ContainerSandboxConfig
from loopforge.adapters.context import BudgetedContextBuilder, CharsPerTokenCounter
from loopforge.adapters.file_tools import WorkspaceFileTools
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.local_sandbox import CommandSpec, ConstrainedLocalSandbox, SandboxLimits
from loopforge.adapters.model_registry import ModelRegistry, ModelRegistryEntry
from loopforge.adapters.routing import TieredRoutingPolicy
from loopforge.adapters.sandbox_tools import SandboxCommandTools, SandboxToolBinding
from loopforge.adapters.scripted import ScriptedModel
from loopforge.adapters.workspace_git_tools import WorkspaceGitTools
from loopforge.application.runtime import Runtime
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.routing import ModelTier, RoutingPolicyConfig
from loopforge.domain.security import SandboxRequirements
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import BudgetLimit, Permission, RiskLevel, WorkspaceId
from loopforge.ports.clock import ClockPort, SleeperPort
from loopforge.ports.model import ModelPort
from loopforge.ports.sandbox import SandboxPort
from loopforge.ports.state_store import StateStorePort
from loopforge.ports.telemetry import TelemetryPort
from loopforge.ports.workspace import WorkspacePort
from loopforge.workloads.repair import (
    REPAIR_MODEL_REQUIREMENTS,
    UNTRUSTED_REPAIR_REQUIREMENTS,
    RepairCheck,
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
    model_tier: ModelTier = ModelTier.ECONOMY
    """Code-owned routing tier for the wired model (scripted default is ECONOMY)."""
    no_progress_limit: int | None = None
    """Operator-tuned stall threshold (PACS-014b); None = ControlPolicy default."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairRuntimeBundle:
    runtime: Runtime
    workspace: WorkspacePort
    sandbox: SandboxPort
    models: tuple[ModelPort, ...] = ()
    """Every registry-registered model whose lifecycle the bundle owns."""

    def close(self) -> None:
        """Release sandbox and model resources best-effort.

        The composition root owns the sandbox lifecycle (an exceptional run
        must never leak an in-flight workload container) and the lifecycle of
        every registered model: routing may serve turns from any registry
        entry, so close() fans out over all of them — closing only
        ``runtime.model`` would leak a routed live adapter's HTTP client.
        Callers should pair ``runtime.run(...)`` with ``close()`` in a
        ``finally`` block.
        """
        destroy = getattr(self.sandbox, "destroy", None)
        if callable(destroy):
            destroy()
        seen: set[int] = set()
        for model in (*self.models, self.runtime.model):
            if id(model) in seen:
                continue
            seen.add(id(model))
            close_model = getattr(model, "close", None)
            if callable(close_model):
                close_model()


def build_trusted_repair_runtime(
    task: RepairTask,
    *,
    workspaces_dir: str | Path,
    deps: RepairRuntimeDeps,
    checks: tuple[RepairCheck, ...] = (),
) -> RepairRuntimeBundle:
    """Wire the deterministic repair stack on the trusted local sandbox.

    This path executes code-defined fixtures through the full runtime with a
    scripted model; it does not claim hostile-code isolation. Untrusted
    fixture execution must use :func:`build_container_repair_runtime`.
    ``checks`` are code-owned acceptance hooks (e.g. the benchmark
    ambiguous-success edge probe), never repository or model content.
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
        checks=checks,
    )


def build_adopted_repair_runtime(  # noqa: PLR0913 - composition roots keep authority explicit
    task: RepairTask,
    *,
    repository: str | Path,
    deps: RepairRuntimeDeps,
    container_image: str | None = None,
    environment: Mapping[str, str] | None = None,
    limits: SandboxLimits | None = None,
    approval_required_for: frozenset[str] | None = None,
) -> RepairRuntimeBundle:
    """Run a repair task against an existing checkout, without copying it.

    This is the dogfood path: the checkout is adopted at its current HEAD and
    all model edits remain visible in that checkout.  Callers must provide a
    code-owned command allowlist and acceptance contract; repository content
    cannot widen either one. The sandbox environment and container resource
    limits are likewise caller-owned (operator authority): they default to an
    empty environment and a 2 GiB container memory ceiling, and can only ever
    be set explicitly by the wiring caller, never by repository content.
    """
    workspace = GitWorkspaceManager(Path(repository).parent).adopt_existing(
        repository, workspace_id=WorkspaceId(task.task_id)
    )
    sandbox_environment = dict(environment) if environment is not None else {}
    if container_image:
        sandbox = ContainerSandbox(
            workspace.root,
            config=ContainerSandboxConfig(
                image=container_image,
                commands=tuple(repair_command_specs(task.commands)),
                environment=sandbox_environment,
                limits=limits or SandboxLimits(max_memory_bytes=2 * 1024 * 1024 * 1024),
            ),
        )
    else:
        sandbox = ConstrainedLocalSandbox(
            workspace.root,
            commands=repair_command_specs(task.commands),
            environment=sandbox_environment,
        )
    return _repair_bundle(
        task,
        workspace=workspace,
        sandbox=sandbox,
        requirements=SandboxRequirements(),
        deps=deps,
        approval_required_for=approval_required_for or frozenset(),
    )


def build_container_repair_runtime(
    task: RepairTask,
    *,
    image: str,
    workspaces_dir: str | Path,
    deps: RepairRuntimeDeps,
    checks: tuple[RepairCheck, ...] = (),
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
        checks=checks,
    )


def _materialize_workspace(task: RepairTask, *, workspaces_dir: str | Path) -> WorkspacePort:
    manager = GitWorkspaceManager(workspaces_dir)
    return manager.materialize(task.fixture)


def _repair_bundle(  # noqa: PLR0913 - composition roots keep authority explicit
    task: RepairTask,
    *,
    workspace: WorkspacePort,
    sandbox: SandboxPort,
    requirements: SandboxRequirements,
    deps: RepairRuntimeDeps,
    approval_required_for: frozenset[str] = frozenset(),
    checks: tuple[RepairCheck, ...] = (),
) -> RepairRuntimeBundle:
    tools: NamedToolExecutor = CompositeToolExecutor(
        (
            WorkspaceFileTools(sandbox, workspace.root),
            WorkspaceGitTools(workspace),
            SandboxCommandTools(
                sandbox,
                repair_command_bindings(task.commands, requirements=requirements),
            ),
        )
    )
    if approval_required_for:
        # Operator-tightened authority (PACS-014): the named tools quiesce
        # the run on a durable ApprovalRequested; unknown names fail closed
        # here at wiring time, before any run starts.
        tools = ApprovalGateTools(tools, required_for=approval_required_for)
    model = deps.model if deps.model is not None else ScriptedModel(scripted_repair_actions(task))
    # The wired model is registered with its honest capabilities and routed
    # through the tiered policy against the workload's code-owned model
    # contract; with a single entry the policy deterministically retains it
    # (or stops the run ROUTE_NO_COMPATIBLE_MODEL if it cannot satisfy the
    # requirements — fail closed, never route to an incompatible model).
    registry = ModelRegistry((ModelRegistryEntry(model=model, tier=deps.model_tier),))
    router = TieredRoutingPolicy(
        registry,
        config=RoutingPolicyConfig(
            requirements=REPAIR_MODEL_REQUIREMENTS,
            default_tier=deps.model_tier,
        ),
    )
    runtime = Runtime(
        model=model,
        router=router,
        tools=tools,
        verifier=RepairVerifier(sandbox, workspace, task.acceptance, hooks=checks),
        store=deps.store,
        control=ControlPolicy(
            deps.budget or BudgetLimit(max_cost_usd=1.0, max_iterations=8),
            no_progress_limit=deps.no_progress_limit or 3,
        ),
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
    return RepairRuntimeBundle(
        runtime=runtime,
        workspace=workspace,
        sandbox=sandbox,
        models=tuple(entry.model for entry in registry.entries),
    )
