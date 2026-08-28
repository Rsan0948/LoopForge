from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from loopforge.domain.artifacts import (
    ArtifactKind,
    validate_artifact_content,
    validate_artifact_label,
)
from loopforge.domain.state import RunState


class ArtifactContractError(TypeError):
    """Raised when an artifact collector violates the runtime response contract."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RunArtifact:
    """One workload-supplied evidence artifact for durable recording.

    Artifacts are evidence, never authority: they are persisted next to
    verification events and never feed back into runtime decisions.
    """

    kind: ArtifactKind
    label: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArtifactKind):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "artifact kind must be an ArtifactKind"
            raise TypeError(msg)
        validate_artifact_label(self.label)
        validate_artifact_content(self.content)


class ArtifactCollectorPort(Protocol):
    """Optional, workload-agnostic seam for collecting durable run evidence.

    The runtime core stays workload-agnostic: workloads bind through this port
    to record exact patches, snapshots, and other verification evidence as
    durable events after each verification.
    """

    def collect(self, state: RunState) -> tuple[RunArtifact, ...]: ...
