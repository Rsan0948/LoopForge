"""Composition wiring for the orchestrated (multi-worker) repair path.

The single-runtime ``repair-demo`` stays the untouched reference behavior;
this module wires the *benchmarkable* multi-agent path (PACS-013): one
code-owned integration workspace, one isolated linked worktree per worker,
per-worker budget shares partitioned from the global limit, per-worker routed
model selection, and durable worker lifecycle events on the orchestrator's
run stream. Multi-agent execution is opt-in, never a default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loopforge.adapters.composite_tools import CompositeToolExecutor
from loopforge.adapters.container_sandbox import ContainerSandbox, ContainerSandboxConfig
from loopforge.adapters.context import BudgetedContextBuilder, CharsPerTokenCounter
from loopforge.adapters.file_tools import WorkspaceFileTools
from loopforge.adapters.git_workspace import GitWorkspace, GitWorkspaceManager
from loopforge.adapters.local_sandbox import CommandSpec, ConstrainedLocalSandbox
from loopforge.adapters.model_registry import ModelRegistry, ModelRegistryEntry
from loopforge.adapters.routing import TieredRoutingPolicy
from loopforge.adapters.sandbox_tools import SandboxCommandTools
from loopforge.adapters.scripted import ScriptedModel
from loopforge.adapters.workspace_git_tools import WorkspaceGitTools
from loopforge.application.orchestrator import Orchestrator, WorkerBinding
from loopforge.application.runtime import Runtime
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.orchestration import partition_budget
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.routing import RoutingPolicyConfig
from loopforge.domain.security import SandboxRequirements
from loopforge.domain.types import BudgetLimit, Permission, WorkerId
from loopforge.entrypoints.repair import (
    RepairRuntimeDeps,
    repair_command_bindings,
    repair_command_specs,
)
from loopforge.ports.model import ModelPort
from loopforge.ports.sandbox import SandboxPort
from loopforge.ports.workspace import WorkspacePort
from loopforge.workloads.repair import (
    REPAIR_MODEL_REQUIREMENTS,
    UNTRUSTED_REPAIR_REQUIREMENTS,
    OrchestratedRepairTask,
    RepairContextBuilder,
    RepairVerifier,
    WorkerRepairAssignment,
    WorkspaceArtifactCollector,
    scripted_repair_actions,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class OrchestratedRepairBundle:
    """The wired orchestrated stack plus every resource it owns."""

    orchestrator: Orchestrator
    integration_workspace: WorkspacePort
    sandboxes: tuple[SandboxPort, ...]
    models: tuple[ModelPort, ...]
    """Every model serving any worker (deduplicated at close)."""

    def close(self) -> None:
        """Release sandboxes and model clients best-effort.

        Mirrors the ``RepairRuntimeBundle.close()`` contract (PACS-012): the
        composition root owns the sandbox lifecycle and the lifecycle of
        every registered model — a shared live adapter serving several
        workers is closed exactly once.
        """
        for sandbox in self.sandboxes:
            destroy = getattr(sandbox, "destroy", None)
            if callable(destroy):
                destroy()
        seen: set[int] = set()
        for model in self.models:
            if id(model) in seen:
                continue
            seen.add(id(model))
            close_model = getattr(model, "close", None)
            if callable(close_model):
                close_model()


def build_orchestrated_repair_runtime(
    task: OrchestratedRepairTask,
    *,
    workspaces_dir: str | Path,
    deps: RepairRuntimeDeps,
    container_image: str | None = None,
) -> OrchestratedRepairBundle:
    """Wire the decomposed repair task: isolated workers + integration verify.

    With ``container_image`` set, worker and integration sandboxes bind with
    ``UNTRUSTED_REPAIR_REQUIREMENTS`` against ``ContainerSandbox`` (failing
    closed anywhere container-grade isolation is unavailable); otherwise the
    trusted local sandbox executes the code-defined fixture with honest
    capability reporting, exactly like the single-runtime path.
    """
    requirements = UNTRUSTED_REPAIR_REQUIREMENTS if container_image else SandboxRequirements()
    manager = GitWorkspaceManager(workspaces_dir)
    integration = manager.materialize(task.fixture)
    global_budget = deps.budget or BudgetLimit(max_cost_usd=2.0, max_iterations=8)
    shares = partition_budget(global_budget, len(task.assignments))

    sandboxes: list[SandboxPort] = []
    models: list[ModelPort] = []
    bindings: list[WorkerBinding] = []
    for assignment, share in zip(task.assignments, shares, strict=True):
        worktree = manager.add_worker_worktree(integration, worker_id=str(assignment.worker_id))
        sandbox = _sandbox_for(
            worktree,
            commands=repair_command_specs(assignment.task.commands),
            container_image=container_image,
        )
        sandboxes.append(sandbox)
        model = deps.model or ScriptedModel(scripted_repair_actions(assignment.task))
        models.append(model)
        runtime = _worker_runtime(
            assignment,
            worktree=worktree,
            sandbox=sandbox,
            requirements=requirements,
            model=model,
            share=share,
            deps=deps,
        )
        bindings.append(
            WorkerBinding(
                worker_id=assignment.worker_id,
                workspace_id=assignment.workspace_id,
                objective=assignment.task.objective,
                budget_share_cost_usd=share.max_cost_usd,
                runtime=runtime,
                reconcile=_reconcile_worker(
                    integration=integration, worktree=worktree, assignment=assignment
                ),
            )
        )

    integration_sandbox = _sandbox_for(
        integration,
        commands=repair_command_specs(task.commands),
        container_image=container_image,
    )
    sandboxes.append(integration_sandbox)
    orchestrator = Orchestrator(
        store=deps.store,
        clock=deps.clock,
        verifier=RepairVerifier(integration_sandbox, integration, task.acceptance),
        budget=global_budget,
        workers=tuple(bindings),
        telemetry=deps.telemetry,
        artifacts=WorkspaceArtifactCollector(integration),
        max_workers=max(4, len(task.assignments)),
    )
    return OrchestratedRepairBundle(
        orchestrator=orchestrator,
        integration_workspace=integration,
        sandboxes=tuple(sandboxes),
        models=tuple(models),
    )


def _sandbox_for(
    workspace: WorkspacePort,
    *,
    commands: list[CommandSpec],
    container_image: str | None,
) -> SandboxPort:
    if container_image is not None:
        return ContainerSandbox(
            workspace.root,
            config=ContainerSandboxConfig(
                image=container_image,
                commands=tuple(commands),
                environment={},
            ),
        )
    return ConstrainedLocalSandbox(workspace.root, commands=commands, environment={})


def _worker_runtime(  # noqa: PLR0913 - composition wiring keeps every seam explicit
    assignment: WorkerRepairAssignment,
    *,
    worktree: GitWorkspace,
    sandbox: SandboxPort,
    requirements: SandboxRequirements,
    model: ModelPort,
    share: BudgetLimit,
    deps: RepairRuntimeDeps,
) -> Runtime:
    task = assignment.task
    tools = CompositeToolExecutor(
        (
            WorkspaceFileTools(sandbox, worktree.root),
            WorkspaceGitTools(worktree),
            SandboxCommandTools(
                sandbox,
                repair_command_bindings(task.commands, requirements=requirements),
            ),
        )
    )
    # Per-worker routing through the shared code-owned requirements contract:
    # each worker gets its own single-entry registry and tiered policy (a
    # shared live adapter is registered per worker, never duplicated in one
    # registry), so worker-role model selection stays a wiring-time decision.
    registry = ModelRegistry((ModelRegistryEntry(model=model, tier=deps.model_tier),))
    router = TieredRoutingPolicy(
        registry,
        config=RoutingPolicyConfig(
            requirements=REPAIR_MODEL_REQUIREMENTS,
            default_tier=deps.model_tier,
        ),
    )
    return Runtime(
        model=model,
        router=router,
        tools=tools,
        verifier=RepairVerifier(sandbox, worktree, task.acceptance),
        store=deps.store,
        control=ControlPolicy(share),
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
        artifacts=WorkspaceArtifactCollector(worktree),
        worker_id=WorkerId(str(assignment.worker_id)),
        verify_read_only_turns=bool(deps.verify_read_only_turns),
    )


def _reconcile_worker(
    *,
    integration: GitWorkspace,
    worktree: GitWorkspace,
    assignment: WorkerRepairAssignment,
) -> Callable[[], str | None]:
    """Spawn-order reconciliation: commit the verified patch, then merge.

    Returns the merge revision, or ``None`` when the merge conflicted and was
    aborted — the orchestrator records that outcome durably and stops
    explicitly. Host-side Git stays behind the fingerprint-guarded adapter.
    """

    def reconcile() -> str | None:
        GitWorkspaceManager.commit_worker(
            worktree,
            message=f"worker {assignment.worker_id}: verified repair",
        )
        return GitWorkspaceManager.merge_worker(integration, worker_id=str(assignment.worker_id))

    return reconcile
