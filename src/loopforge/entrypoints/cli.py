from __future__ import annotations

import argparse

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import ObservationContainsVerifier, ScriptedModel, ScriptedTools
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, BudgetLimit, Permission, RiskLevel
from loopforge.ports.tools import ToolResult


def _metadata(
    name: str,
    *,
    risk: RiskLevel,
    side_effect: SideEffectClass,
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
    )


def _demo() -> int:
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
                _metadata(
                    "fix",
                    risk=RiskLevel.LOCAL_WRITE,
                    side_effect=SideEffectClass.LOCAL_WRITE,
                ),
            ],
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=InMemoryEventStore(),
        control=ControlPolicy(BudgetLimit(max_cost_usd=1.0, max_iterations=5)),
        permissions=PermissionPolicy(frozenset({Permission.READ, Permission.LOCAL_WRITE})),
        reliability=ReliabilityPolicy(),
        context=BasicContextBuilder(SystemClock()),
        clock=SystemClock(),
        sleeper=SystemSleeper(),
    )
    state = runtime.run("Repair authentication regression")
    print(
        f"run={state.run_id} status={state.status.value} "
        f"iterations={state.iteration} cost=${state.cost_usd:.2f}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="loopforge")
    parser.add_argument("command", choices=["demo"])
    args = parser.parse_args()
    if args.command == "demo":
        return _demo()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
