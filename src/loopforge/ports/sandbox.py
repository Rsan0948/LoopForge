from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.security import SandboxCapabilities


class SandboxError(RuntimeError):
    """Base error for sandbox policy/execution failures."""


class SandboxPolicyError(SandboxError):
    """Raised when a requested operation violates sandbox policy."""


class SandboxPathError(SandboxPolicyError):
    """Raised when a filesystem request escapes or violates workspace policy."""


class SandboxTimeoutError(SandboxError):
    """Raised when a command exceeds its configured wall-clock timeout."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxCommandResult:
    exit_code: int
    stdout: str
    stderr: str
    succeeded: bool


class SandboxPort(Protocol):
    @property
    def capabilities(self) -> SandboxCapabilities: ...

    def read_text(self, relative_path: str) -> str: ...

    def write_text(self, relative_path: str, content: str) -> None: ...

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult: ...
