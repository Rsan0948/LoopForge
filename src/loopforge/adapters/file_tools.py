"""Safe workspace file tools exposed through the normal tool registry contract.

Read/search/edit operations for the repair workload. Reads and writes are
delegated to the bound ``SandboxPort`` file API so traversal, symlink, and
byte-limit defenses stay adapter-enforced; search walks the assigned workspace
root without following symlinks and skips the Git metadata directory. All
tool metadata is code-owned (AGENTS.md rule 4): model output may select a
tool and supply arguments, but can never alter risk, permission, retry, or
idempotency semantics.

Search is a deliberately literal substring match: it is deterministic, and no
model-supplied pattern can cause regex denial of service.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import Permission, RiskLevel
from loopforge.ports.sandbox import (
    SandboxError,
    SandboxPolicyError,
    SandboxPort,
    SandboxTimeoutError,
)
from loopforge.ports.tools import ToolExecutionRequest, ToolResult, UnknownToolError

_READ_FILE: Final = "read_file"
_SEARCH_FILES: Final = "search_files"
_WRITE_FILE: Final = "write_file"
_EDIT_FILE: Final = "edit_file"
_GIT_DIR_NAME: Final = ".git"
_MAX_LINE_RENDER: Final = 500


def _tool_metadata(
    name: str,
    *,
    risk: RiskLevel,
    side_effect: SideEffectClass,
    retry: RetryClass,
    idempotency: IdempotencyClass,
) -> ToolMetadata:
    required_permission = {
        RiskLevel.READ_ONLY: Permission.READ,
        RiskLevel.LOCAL_WRITE: Permission.LOCAL_WRITE,
    }[risk]
    return ToolMetadata(
        name=name,
        risk=risk,
        required_permission=required_permission,
        side_effect=side_effect,
        retry=retry,
        idempotency=idempotency,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FileToolLimits:
    """Code-owned bounds for workspace file tools."""

    max_results: int = 200
    max_file_bytes: int = 1_000_000
    max_query_bytes: int = 256

    def __post_init__(self) -> None:
        if min(self.max_results, self.max_file_bytes, self.max_query_bytes) <= 0:
            msg = "file tool limits must be positive"
            raise ValueError(msg)


class WorkspaceFileTools:
    """Safe file read/search/edit tools bound to a sandboxed workspace."""

    def __init__(
        self,
        sandbox: SandboxPort,
        root: str | Path,
        *,
        limits: FileToolLimits | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._root = Path(root).resolve()
        if not self._root.is_dir():
            msg = "file tools root must be an existing directory"
            raise ValueError(msg)
        self._limits = limits or FileToolLimits()
        self._metadata = {
            _READ_FILE: _tool_metadata(
                _READ_FILE,
                risk=RiskLevel.READ_ONLY,
                side_effect=SideEffectClass.READ_ONLY,
                retry=RetryClass.SAFE,
                idempotency=IdempotencyClass.NOT_APPLICABLE,
            ),
            _SEARCH_FILES: _tool_metadata(
                _SEARCH_FILES,
                risk=RiskLevel.READ_ONLY,
                side_effect=SideEffectClass.READ_ONLY,
                retry=RetryClass.SAFE,
                idempotency=IdempotencyClass.NOT_APPLICABLE,
            ),
            _WRITE_FILE: _tool_metadata(
                _WRITE_FILE,
                risk=RiskLevel.LOCAL_WRITE,
                side_effect=SideEffectClass.LOCAL_WRITE,
                retry=RetryClass.SAFE,
                idempotency=IdempotencyClass.NATURAL,
            ),
            _EDIT_FILE: _tool_metadata(
                _EDIT_FILE,
                risk=RiskLevel.LOCAL_WRITE,
                side_effect=SideEffectClass.LOCAL_WRITE,
                # A failed edit is ambiguous on replay (the target text may or
                # may not have been replaced), so retries fail closed instead
                # of claiming natural idempotency (AGENTS.md rule 5).
                retry=RetryClass.NEVER,
                idempotency=IdempotencyClass.NONE,
            ),
        }

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._metadata)

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        try:
            return self._metadata[tool_name]
        except KeyError as exc:
            msg = f"unknown file tool: {tool_name}"
            raise UnknownToolError(msg) from exc

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        handler = {
            _READ_FILE: self._read_file,
            _SEARCH_FILES: self._search_files,
            _WRITE_FILE: self._write_file,
            _EDIT_FILE: self._edit_file,
        }.get(request.proposal.tool_name)
        if handler is None:
            msg = f"unknown file tool: {request.proposal.tool_name}"
            raise UnknownToolError(msg)
        return handler(request)

    def _read_file(self, request: ToolExecutionRequest) -> ToolResult:
        path = self._required_argument(request, "path")
        if isinstance(path, ToolResult):
            return path
        try:
            content = self._sandbox.read_text(path)
        except SandboxError as exc:
            return _sandbox_failure(exc)
        return ToolResult(ok=True, observation=content)

    def _search_files(self, request: ToolExecutionRequest) -> ToolResult:
        query = self._required_argument(request, "query")
        if isinstance(query, ToolResult):
            return query
        if len(query.encode("utf-8")) > self._limits.max_query_bytes:
            return _argument_failure("search query exceeds the query byte limit")
        matches: list[str] = []
        truncated = False
        unreadable = 0
        for candidate in self._iter_workspace_files():
            try:
                if candidate.stat().st_size > self._limits.max_file_bytes:
                    continue
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                # Files may vanish or become unreadable between the directory
                # walk and the read; skip them honestly instead of crashing.
                unreadable += 1
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query not in line:
                    continue
                rendered = line[:_MAX_LINE_RENDER]
                relative = candidate.relative_to(self._root).as_posix()
                matches.append(f"{relative}:{line_number}: {rendered}")
                if len(matches) >= self._limits.max_results:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            matches.append(f"loopforge: search truncated after {self._limits.max_results} results")
        if unreadable:
            matches.append(f"loopforge: search skipped {unreadable} unreadable file(s)")
        observation = "\n".join(matches) if matches else f"no matches for {query!r}"
        return ToolResult(ok=True, observation=observation)

    def _write_file(self, request: ToolExecutionRequest) -> ToolResult:
        path = self._required_argument(request, "path")
        if isinstance(path, ToolResult):
            return path
        content = self._required_argument(request, "content", allow_empty=True)
        if isinstance(content, ToolResult):
            return content
        try:
            self._sandbox.write_text(path, content)
        except SandboxError as exc:
            return _sandbox_failure(exc)
        return ToolResult(ok=True, observation=f"wrote {path} ({len(content)} characters)")

    def _edit_file(self, request: ToolExecutionRequest) -> ToolResult:
        resolved = self._edit_arguments(request)
        if isinstance(resolved, ToolResult):
            return resolved
        path, old, new = resolved
        try:
            content = self._sandbox.read_text(path)
        except SandboxError as exc:
            return _sandbox_failure(exc)
        occurrences = content.count(old)
        if occurrences == 0:
            return _argument_failure(f"edit target not found in {path}")
        if occurrences > 1:
            return _argument_failure(f"edit target is not unique in {path}")
        try:
            self._sandbox.write_text(path, content.replace(old, new, 1))
        except SandboxError as exc:
            return _sandbox_failure(exc)
        return ToolResult(ok=True, observation=f"edited {path} (1 replacement)")

    def _edit_arguments(self, request: ToolExecutionRequest) -> tuple[str, str, str] | ToolResult:
        path = self._required_argument(request, "path")
        if isinstance(path, ToolResult):
            return path
        old = self._required_argument(request, "old")
        if isinstance(old, ToolResult):
            return old
        new = self._required_argument(request, "new", allow_empty=True)
        if isinstance(new, ToolResult):
            return new
        return path, old, new

    def _required_argument(
        self, request: ToolExecutionRequest, name: str, *, allow_empty: bool = False
    ) -> str | ToolResult:
        value = request.proposal.arguments.get(name)
        # Boundary validation is intentional: model output may violate the
        # Mapping[str, str] argument contract.
        if not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            return _argument_failure(f"tool argument {name!r} must be a string")
        if not allow_empty and not value.strip():
            return _argument_failure(f"tool argument {name!r} is required")
        return value

    def _iter_workspace_files(self) -> Iterator[Path]:
        for dirpath, dirnames, filenames in self._root.walk():
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name != _GIT_DIR_NAME and not (dirpath / name).is_symlink()
            )
            for name in sorted(filenames):
                candidate = dirpath / name
                if not candidate.is_symlink() and candidate.is_file():
                    yield candidate


def _argument_failure(observation: str) -> ToolResult:
    return ToolResult(
        ok=False,
        observation=observation,
        error_code="TOOL_ARGUMENTS",
        failure_class=ToolFailureClass.PERMANENT,
    )


def _sandbox_failure(exc: SandboxError) -> ToolResult:
    if isinstance(exc, SandboxTimeoutError):
        return ToolResult(
            ok=False,
            observation=str(exc),
            error_code="SANDBOX_TIMEOUT",
            failure_class=ToolFailureClass.TRANSIENT,
        )
    if isinstance(exc, SandboxPolicyError):
        return ToolResult(
            ok=False,
            observation=str(exc),
            error_code="SANDBOX_POLICY",
            failure_class=ToolFailureClass.PERMANENT,
        )
    return ToolResult(
        ok=False,
        observation=str(exc),
        error_code="SANDBOX_EXECUTION",
        failure_class=ToolFailureClass.PERMANENT,
    )
