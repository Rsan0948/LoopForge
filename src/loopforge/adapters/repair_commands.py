"""Lower code-owned repair commands into sandbox allowlist specs and tool bindings."""

from __future__ import annotations

from loopforge.adapters.local_sandbox import CommandSpec
from loopforge.adapters.sandbox_tools import SandboxToolBinding
from loopforge.domain.security import SandboxRequirements
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import Permission, RiskLevel
from loopforge.workloads.repair import RepairCommand


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
