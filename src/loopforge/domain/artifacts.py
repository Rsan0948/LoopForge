from __future__ import annotations

from enum import StrEnum
from typing import Final

MAX_ARTIFACT_CONTENT_BYTES: Final = 4_000_000
"""Hard byte budget for one persisted artifact payload.

Enforced at the port boundary, at event construction, and at codec decode so
no collector (present or future) can grow the authoritative event store or
replay memory without bound.
"""

_MAX_ARTIFACT_LABEL_LENGTH: Final = 256


def validate_artifact_label(label: str) -> None:
    """Enforce the artifact-label contract shared by ports, events, and codec."""
    if not isinstance(label, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = "artifact label must be a string"
        raise TypeError(msg)
    if not label.strip():
        msg_2 = "artifact label cannot be empty"
        raise ValueError(msg_2)
    if len(label) > _MAX_ARTIFACT_LABEL_LENGTH or any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in label
    ):
        msg_3 = "artifact label must not contain control characters or exceed 256 characters"
        raise ValueError(msg_3)


def validate_artifact_content(content: str) -> None:
    """Enforce the artifact-content contract shared by ports, events, and codec."""
    if not isinstance(content, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = "artifact content must be a string"
        raise TypeError(msg)
    if len(content.encode("utf-8", errors="surrogateescape")) > MAX_ARTIFACT_CONTENT_BYTES:
        msg_2 = f"artifact content exceeds the {MAX_ARTIFACT_CONTENT_BYTES}-byte budget"
        raise ValueError(msg_2)


class ArtifactKind(StrEnum):
    """Closed vocabulary of durable run-evidence artifact kinds.

    Artifacts are workload-supplied evidence recorded alongside verification so
    a replayable run captures exactly what the workspace looked like. The
    vocabulary is code-owned and closed: workload or model content can never
    invent new kinds.
    """

    WORKSPACE_SNAPSHOT = "workspace_snapshot"
