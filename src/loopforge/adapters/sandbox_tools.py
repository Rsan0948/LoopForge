from __future__ import annotations

from dataclasses import dataclass

from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.security import SandboxRequirements
from loopforge.domain.tooling import ToolMetadata
from loopforge.ports.sandbox import (
    SandboxError,
    SandboxPolicyError,
    SandboxPort,
    SandboxTimeoutError,
)
from loopforge.ports.tools import ToolExecutionRequest, ToolResult, UnknownToolError


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxToolBinding:
    metadata: ToolMetadata
    command_name: str
    requirements: SandboxRequirements = SandboxRequirements()


class SandboxCommandTools:
    """Expose fixed sandbox commands through the normal code-owned tool registry contract."""

    def __init__(self, sandbox: SandboxPort, bindings: list[SandboxToolBinding]) -> None:
        self._sandbox = sandbox
        self._bindings = {item.metadata.name: item for item in bindings}
        if len(self._bindings) != len(bindings):
            raise ValueError("sandbox tool names must be unique")
        for binding in bindings:
            try:
                sandbox.capabilities.require(binding.requirements)
            except ValueError as exc:
                raise ValueError(
                    f"sandbox cannot satisfy tool {binding.metadata.name!r}: {exc}"
                ) from exc

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        try:
            return self._bindings[tool_name].metadata
        except KeyError as exc:
            raise UnknownToolError(f"unknown sandbox tool: {tool_name}") from exc

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        binding = self._bindings.get(request.proposal.tool_name)
        if binding is None:
            raise UnknownToolError(f"unknown sandbox tool: {request.proposal.tool_name}")
        try:
            result = self._sandbox.run(
                binding.command_name, timeout_seconds=request.timeout_seconds
            )
        except SandboxTimeoutError as exc:
            return ToolResult(
                ok=False,
                observation=str(exc),
                error_code="SANDBOX_TIMEOUT",
                failure_class=ToolFailureClass.TRANSIENT,
            )
        except SandboxPolicyError as exc:
            return ToolResult(
                ok=False,
                observation=str(exc),
                error_code="SANDBOX_POLICY",
                failure_class=ToolFailureClass.PERMANENT,
            )
        except SandboxError as exc:
            return ToolResult(
                ok=False,
                observation=str(exc),
                error_code="SANDBOX_EXECUTION",
                failure_class=ToolFailureClass.PERMANENT,
            )
        observation = (
            f"exit_code={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        if result.succeeded:
            return ToolResult(ok=True, observation=observation)
        return ToolResult(
            ok=False,
            observation=observation,
            error_code=f"SANDBOX_EXIT_{result.exit_code}",
            failure_class=ToolFailureClass.PERMANENT,
        )
