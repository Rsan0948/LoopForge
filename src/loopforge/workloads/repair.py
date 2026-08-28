"""Software-repair reference workload bound through existing ports.

Everything in this module is code-owned workload authority:

- ``RepairTask`` couples a fixture repository, predefined sandbox commands,
  and acceptance criteria — repository or model content can never widen any
  of them (AGENTS.md rules 14 and 16);
- ``RepairVerifier`` grants success only when independent deterministic
  checks pass: required commands must succeed *through the sandbox* and the
  patch constraints must hold. Model claims, expected observations, and
  evaluator output are never consulted, so false success is rejected by
  construction, not by prompt;
- ``WorkspaceArtifactCollector`` turns the exact workspace patch and
  changed-file inventory into durable evidence artifacts;
- ``RepairContextBuilder`` demotes journaled tool observations to
  ``UNTRUSTED_CONTENT`` so repository output enters model context at the
  correct trust class (rule 16);
- ``UNTRUSTED_REPAIR_REQUIREMENTS`` is the code-owned sandbox contract for
  untrusted fixture/build execution: bindings that declare it fail closed
  anywhere container-grade isolation is unavailable (rules 13-15).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import ArtifactKind
from loopforge.domain.context import ContextItem, ModelContext, ModelRole
from loopforge.domain.context_lifecycle import ContextAccounting, ContextTokenBudget
from loopforge.domain.security import SandboxRequirements, TrustClass
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, ContextItemId
from loopforge.domain.verification import CheckOutcome, compose_check_outcomes
from loopforge.domain.workspace import AcceptanceCriteria, FixtureSpec
from loopforge.ports.artifacts import RunArtifact
from loopforge.ports.context import ContextAccountingSource, ContextBuilderPort
from loopforge.ports.model import ModelToolSpec
from loopforge.ports.sandbox import SandboxError, SandboxPort
from loopforge.ports.verifier import VerificationResult
from loopforge.ports.workspace import WorkspaceError, WorkspacePort

UNTRUSTED_REPAIR_REQUIREMENTS: Final = SandboxRequirements(
    process_filesystem_isolated=True,
    network_isolated=True,
)
"""Sandbox contract for untrusted fixture/build execution (PACS-009 boundary)."""

_DETAIL_BUDGET: Final = 300
_OBSERVATION_ITEM_KEY: Final = "observation"


class RepairCommandKind(StrEnum):
    """Closed vocabulary of predefined repair command kinds."""

    TEST = "test"
    LINT = "lint"
    TYPECHECK = "typecheck"
    BUILD = "build"


class RepairCheckContractError(TypeError):
    """Raised when a repair check hook violates the check outcome contract."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairCommand:
    """One code-owned, predefined command the repair workload may run."""

    kind: RepairCommandKind
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 60.0
    cpu_seconds: int = 30

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "repair command name cannot be empty"
            raise ValueError(msg)
        if not self.argv or not Path(self.argv[0]).is_absolute():
            msg_2 = "repair command argv must start with an absolute executable path"
            raise ValueError(msg_2)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            msg_3 = "repair command timeout must be positive and finite"
            raise ValueError(msg_3)
        if not math.isfinite(self.cpu_seconds) or self.cpu_seconds <= 0:
            msg_4 = "repair command cpu seconds must be positive and finite"
            raise ValueError(msg_4)


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairTask:
    """Code-owned definition of one deterministic repair task."""

    task_id: str
    objective: str
    fixture: FixtureSpec
    commands: tuple[RepairCommand, ...]
    acceptance: AcceptanceCriteria

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            msg = "repair task_id cannot be empty"
            raise ValueError(msg)
        if not self.objective.strip():
            msg_5 = "repair task objective cannot be empty"
            raise ValueError(msg_5)
        if not self.acceptance.required_commands:
            # An acceptance contract demanding nothing would be satisfied by
            # nothing: at least one sandbox command must gate success.
            msg_4 = "acceptance criteria must require at least one command"
            raise ValueError(msg_4)
        names = [command.name for command in self.commands]
        if len(set(names)) != len(names):
            msg_2 = "repair command names must be unique"
            raise ValueError(msg_2)
        unknown = [name for name in self.acceptance.required_commands if name not in names]
        if unknown:
            msg_3 = f"acceptance criteria reference undefined commands: {', '.join(unknown)}"
            raise ValueError(msg_3)


class RepairCheck(Protocol):
    """Acceptance-criteria verifier hook seam.

    Hooks are code-owned callables supplied at wiring time; they can never be
    provided by repository or model content.
    """

    def __call__(self, sandbox: SandboxPort, workspace: WorkspacePort) -> CheckOutcome: ...


class RepairVerifier:
    """Deterministic verifier for repair tasks.

    Verifier truth is code-owned: the verifier runs the required commands
    through the sandbox and evaluates patch constraints against the actual
    workspace. It never reads run state, so a model's claim of success (or an
    ``expected_observation``) can never influence the verdict.
    """

    def __init__(
        self,
        sandbox: SandboxPort,
        workspace: WorkspacePort,
        criteria: AcceptanceCriteria,
        *,
        hooks: tuple[RepairCheck, ...] = (),
    ) -> None:
        self._sandbox = sandbox
        self._workspace = workspace
        self._criteria = criteria
        self._hooks = hooks

    def verify(self, state: RunState) -> VerificationResult:
        del state  # verification truth comes from the workspace, never model-claimed state
        outcomes = [self._command_outcome(name) for name in self._criteria.required_commands]
        outcomes.append(self._patch_outcome_checked())
        for index, hook in enumerate(self._hooks, start=1):
            outcomes.append(self._hook_outcome(index, hook))
        composite = compose_check_outcomes(tuple(outcomes))
        return VerificationResult(
            passed=composite.passed, summary=composite.summary, score=composite.score
        )

    def _hook_outcome(self, index: int, hook: RepairCheck) -> CheckOutcome:
        name = f"hook:{getattr(hook, '__name__', None) or f'hook_{index}'}"
        try:
            outcome = hook(self._sandbox, self._workspace)
        except Exception as exc:
            # A failing hook is a failed check, never a crashed run: verifier
            # truth fails closed and the run still reaches a durable verdict.
            return CheckOutcome(
                name=name,
                passed=False,
                detail=_bounded(f"hook raised {type(exc).__name__}: {exc}"),
            )
        # Boundary validation is intentional: hooks may violate the contract.
        if not isinstance(outcome, CheckOutcome):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = f"repair check hook returned {type(outcome).__name__}, expected CheckOutcome"
            raise RepairCheckContractError(msg)
        return outcome

    def _patch_outcome_checked(self) -> CheckOutcome:
        try:
            return self._patch_outcome()
        except WorkspaceError as exc:
            return CheckOutcome(
                name="patch_constraints",
                passed=False,
                detail=_bounded(f"workspace error: {exc}"),
            )

    def _command_outcome(self, name: str) -> CheckOutcome:
        try:
            result = self._sandbox.run(name)
        except SandboxError as exc:
            return CheckOutcome(
                name=f"command:{name}",
                passed=False,
                detail=f"sandbox error: {_bounded(str(exc))}",
            )
        return CheckOutcome(
            name=f"command:{name}",
            passed=result.succeeded,
            detail=f"exit_code={result.exit_code}",
        )

    def _patch_outcome(self) -> CheckOutcome:
        constraints = self._criteria.patch
        files = self._workspace.status().files
        violations: list[str] = []
        if constraints.require_change and not files:
            violations.append("no workspace changes")
        outside = [path for path in files if not _path_allowed(path, constraints.allowed_prefixes)]
        if outside:
            violations.append("paths outside allowed prefixes: " + ", ".join(outside))
        if constraints.max_changed_files is not None and len(files) > constraints.max_changed_files:
            violations.append(
                f"{len(files)} changed files exceed limit {constraints.max_changed_files}"
            )
        if violations:
            return CheckOutcome(
                name="patch_constraints",
                passed=False,
                detail=_bounded("; ".join(violations)),
            )
        changed = ", ".join(files) if files else "none"
        return CheckOutcome(
            name="patch_constraints",
            passed=True,
            detail=_bounded(f"files changed: {changed}"),
        )


class WorkspaceArtifactCollector:
    """Collects the exact workspace patch and inventory as durable evidence.

    Snapshots fail loudly when they exceed the byte budget: a truncated patch
    can never masquerade as the exact patch evidence.
    """

    def __init__(self, workspace: WorkspacePort, *, max_artifact_bytes: int = 1_000_000) -> None:
        if max_artifact_bytes <= 0:
            msg = "max_artifact_bytes must be positive"
            raise ValueError(msg)
        self._workspace = workspace
        self._max_artifact_bytes = max_artifact_bytes

    def collect(self, state: RunState) -> tuple[RunArtifact, ...]:
        del state  # evidence comes from the workspace, not from model-claimed state
        status = self._workspace.status()
        content = "\n".join(
            [
                f"workspace_id={self._workspace.workspace_id}",
                f"base_revision={self._workspace.base_revision}",
                "changed_files=" + (",".join(status.changed) or "-"),
                "untracked_files=" + (",".join(status.untracked) or "-"),
                "",
                self._workspace.diff(),
            ]
        )
        if len(content.encode("utf-8")) > self._max_artifact_bytes:
            msg = "workspace snapshot exceeds the artifact byte budget"
            raise WorkspaceError(msg)
        return (
            RunArtifact(
                kind=ArtifactKind.WORKSPACE_SNAPSHOT,
                label=f"workspace:{self._workspace.workspace_id}",
                content=content,
            ),
        )


class RepairContextBuilder:
    """Context builder decorator enforcing repair-workload trust classes.

    Tool observations in the repair workload carry repository-influenced
    content (file bytes, search hits, command output). They are demoted to
    ``UNTRUSTED_CONTENT`` before crossing the model boundary; runtime-owned
    items (objective, plan, verifier outcomes) keep their original trust
    classes. Demotion is the only direction this builder ever moves trust.
    """

    def __init__(self, delegate: ContextBuilderPort) -> None:
        self._delegate = delegate

    @property
    def last_accounting(self) -> ContextAccounting | None:
        if isinstance(self._delegate, ContextAccountingSource):
            return self._delegate.last_accounting
        return None

    def build_context(
        self,
        state: RunState,
        *,
        role: ModelRole = ModelRole.CONTROLLER,
        token_budget: ContextTokenBudget | None = None,
    ) -> ModelContext:
        context = self._delegate.build_context(state, role=role, token_budget=token_budget)
        demoted = tuple(self._demote_if_observation(item, state) for item in context.items)
        return replace(context, items=demoted)

    @staticmethod
    def _demote_if_observation(item: ContextItem, state: RunState) -> ContextItem:
        observation_id = ContextItemId(f"{state.run_id}:{_OBSERVATION_ITEM_KEY}")
        if item.trust is TrustClass.DETERMINISTIC_OBSERVATION and item.item_id == observation_id:
            return replace(
                item,
                trust=TrustClass.UNTRUSTED_CONTENT,
                source=replace(
                    item.source,
                    origin=TrustClass.UNTRUSTED_CONTENT,
                    detail=f"{item.source.detail} (untrusted repository output)",
                ),
            )
        return item


def repair_tool_specs(task: RepairTask) -> tuple[ModelToolSpec, ...]:
    """Code-owned catalog describing the repair tools to a live model.

    The catalog only *describes* the tools the runtime has already registered;
    it cannot define or widen authority. Risk, permission, side-effect, retry,
    idempotency, approval, timeout, and sensitivity metadata stay on the
    runtime-owned ``ToolMetadata`` and are re-authorized for every proposal
    (AGENTS.md rule 4). Every argument is string-valued, matching
    ``ActionProposal.arguments``.
    """
    specs = [
        ModelToolSpec(
            name="read_file",
            description="Read the contents of a file in the assigned workspace.",
            parameters=_string_schema("path"),
        ),
        ModelToolSpec(
            name="search_files",
            description=("Search workspace files for a literal substring; returns matching lines."),
            parameters=_string_schema("query"),
        ),
        ModelToolSpec(
            name="write_file",
            description="Write a file in the assigned workspace, replacing any existing content.",
            parameters=_string_schema("path", "content"),
        ),
        ModelToolSpec(
            name="edit_file",
            description=(
                "Replace an exact substring in a workspace file; the old text must occur "
                "exactly once."
            ),
            parameters=_string_schema("path", "old", "new"),
        ),
        ModelToolSpec(
            name="workspace_status",
            description="List files changed relative to the workspace base revision.",
            parameters=_empty_schema(),
        ),
        ModelToolSpec(
            name="workspace_diff",
            description="Show the unified diff of workspace changes against the base revision.",
            parameters=_empty_schema(),
        ),
        ModelToolSpec(
            name="revert_file",
            description="Revert one workspace file to its base-revision content.",
            parameters=_string_schema("path"),
        ),
    ]
    specs.extend(
        ModelToolSpec(
            name=command.name,
            description=(
                f"Run the predefined {command.kind.value} command '{command.name}' inside "
                "the sandbox and return its output."
            ),
            parameters=_empty_schema(),
        )
        for command in task.commands
    )
    return tuple(specs)


def _string_schema(*required: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in required},
        "required": list(required),
        "additionalProperties": False,
    }


def _empty_schema() -> dict[str, object]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def scripted_repair_actions(task: RepairTask) -> list[ActionProposal]:
    """Deterministic scripted-model script that applies the fixture solution.

    Used by the acceptance demo and tests to prove a scripted model can repair
    a fixture repository through the full runtime.
    """
    actions = [
        ActionProposal(
            action_id=ActionId("repair-read-1"),
            tool_name="read_file",
            arguments={"path": task.fixture.files[0].path},
        )
    ]
    actions.extend(
        ActionProposal(
            action_id=ActionId(f"repair-write-{index}"),
            tool_name="write_file",
            arguments={"path": item.path, "content": item.content},
        )
        for index, item in enumerate(task.fixture.solution, start=1)
    )
    return actions


def _bounded(text: str) -> str:
    # Sanitize first: details embed model-influenced content (file names,
    # sandbox error text), and control characters must never forge lines in
    # verifier summaries, durable events, logs, or context items.
    sanitized = "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in text)
    if len(sanitized) <= _DETAIL_BUDGET:
        return sanitized
    return sanitized[: _DETAIL_BUDGET - 3] + "..."


def _path_allowed(path: str, allowed_prefixes: tuple[str, ...]) -> bool:
    if not allowed_prefixes:
        return True
    return any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed_prefixes
    )
