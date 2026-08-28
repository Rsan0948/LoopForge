from __future__ import annotations

from pathlib import Path
from typing import Protocol

from loopforge.domain.types import WorkspaceId
from loopforge.domain.workspace import FixtureSpec, WorkspaceStatus


class WorkspaceError(RuntimeError):
    """Base error for workspace manager/adapter failures."""


class WorkspacePort(Protocol):
    """Adapter-layer boundary for an assigned, revision-tracked workspace.

    Implementations provide the Git status/diff/checkout primitives the repair
    workload needs. The domain never imports Git (AGENTS.md rule 1); every
    operation is confined to the assigned workspace root.
    """

    @property
    def workspace_id(self) -> WorkspaceId: ...

    @property
    def root(self) -> Path: ...

    @property
    def base_revision(self) -> str: ...

    def status(self) -> WorkspaceStatus: ...

    def diff(self) -> str: ...

    def checkout(self, paths: tuple[str, ...]) -> None: ...

    def reset(self) -> None: ...


class WorkspaceManagerPort(Protocol):
    """Materializes code-owned fixture repositories into assigned workspaces."""

    def materialize(
        self, fixture: FixtureSpec, *, workspace_id: WorkspaceId | None = None
    ) -> WorkspacePort: ...
