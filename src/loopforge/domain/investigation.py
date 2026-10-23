"""Strict, lower-trust investigation vocabulary; no execution authority in proposals."""

from dataclasses import dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    NONE = "none"
    ENVIRONMENT = "environment"
    PROVIDER = "provider"
    CONTEXT = "context"
    NO_PROGRESS = "no_progress"
    VERIFICATION = "verification"
    POLICY = "policy"
    UNKNOWN = "unknown"
    PROTOCOL = "protocol"
    HYPOTHESIS = "hypothesis"
    AUTHORITY = "authority"


def relative_path(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and all(
            part
            and part not in {".", "..", ".git", ".env", "secrets", "credentials"}
            and not part.startswith(".env.")
            for part in value.split("/")
        )
        and "\\" not in value
        and not any(ord(c) < 32 for c in value)
    )


@dataclass(frozen=True)
class RecoveryIntent:
    changed_hypothesis: str
    evidence_ids: tuple[str, ...]
    failure: FailureKind

    def __post_init__(self) -> None:
        if (
            not 20 <= len(self.changed_hypothesis.strip()) <= 1000
            or not 1 <= len(self.evidence_ids) <= 12
            or len(set(self.evidence_ids)) != len(self.evidence_ids)
        ):
            msg = "Recovery needs a concrete changed hypothesis and observed evidence"
            raise ValueError(msg)


@dataclass(frozen=True)
class RecoveryAttempt:
    candidate_sha256: str
    intent: RecoveryIntent


@dataclass(frozen=True)
class InspectionRequest:
    path: str = ""
    query: str = ""
    start_line: int = 1
    line_count: int = 80
    offset_bytes: int = 0
    target: str = "baseline"
    candidate_sha256: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.offset_bytes) is not int
            or not 0 <= self.offset_bytes <= 4_000_000
            or self.target not in {"baseline", "candidate"}
            or (
                self.target == "candidate"
                and (
                    len(self.candidate_sha256) != 64
                    or any(c not in "0123456789abcdef" for c in self.candidate_sha256)
                )
            )
            or (self.target == "baseline" and self.candidate_sha256)
        ):
            msg = "Inspection requires a valid cursor and exact candidate identity"
            raise ValueError(msg)
        if (
            (self.path and not relative_path(self.path))
            or len(self.path) > 500
            or len(self.query) > 200
        ):
            msg = "Inspection requires a safe relative path and bounded literal query"
            raise ValueError(msg)
        if (
            type(self.start_line) is not int
            or type(self.line_count) is not int
            or not (1 <= self.start_line <= 1_000_000 and 1 <= self.line_count <= 120)
        ):
            msg = "Inspection line range is outside supported bounds"
            raise ValueError(msg)


@dataclass(frozen=True)
class TaskAssignment:
    objective: str
    files: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 10 <= len(self.objective.strip()) <= 1500 or not (
            1 <= len(self.files) <= 30
            and len(set(self.files)) == len(self.files)
            and all(relative_path(path) for path in self.files)
            and 1 <= len(self.evidence_ids) <= 12
            and len(set(self.evidence_ids)) == len(self.evidence_ids)
        ):
            msg = "Assignment requires an objective, explicit safe deliverables and evidence"
            raise ValueError(msg)


@dataclass(frozen=True)
class ResearchRecord:
    record_id: str
    operation_key: str
    kind: str
    revision: str
    reference: str
    content_sha256: str
    summary: str
    excerpt: str
    truncated: bool = False
    environment_id: str = ""
    environment_ready: bool = False
    baseline_reproduced: bool = False
    failure: FailureKind = FailureKind.NONE
    assignment: TaskAssignment | None = None
    candidate_sha256: str = ""
    next_offset_bytes: int | None = None
    candidate_checkpoint: str = ""
    seed_manifest_sha256: str = ""
    start_offset_bytes: int = 0
    end_offset_bytes: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.start_offset_bytes) is not int
            or type(self.end_offset_bytes) is not int
            or not 0 <= self.start_offset_bytes <= self.end_offset_bytes <= 4_000_000
            or (
                self.next_offset_bytes is not None
                and (
                    type(self.next_offset_bytes) is not int
                    or not 0 <= self.next_offset_bytes <= 4_000_000
                )
            )
        ):
            msg = "Evidence byte range is invalid"
            raise ValueError(msg)
        if self.candidate_checkpoint and (
            not relative_path(self.candidate_checkpoint)
            or len(self.seed_manifest_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.seed_manifest_sha256)
        ):
            msg = "Candidate checkpoint requires its recorded manifest identity"
            raise ValueError(msg)
        if self.kind not in {
            "inventory",
            "source",
            "environment",
            "baseline",
            "retrieval",
            "assignment",
            "worker",
        }:
            msg = "Unsupported investigation record kind"
            raise ValueError(msg)
        if (
            not self.record_id
            or not self.operation_key
            or len(self.summary) > 1000
            or len(self.excerpt.encode()) > 12_000
        ):
            msg = "Invalid investigation record bounds"
            raise ValueError(msg)
        if len(self.content_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.content_sha256
        ):
            msg = "Investigation evidence requires an exact content identity"
            raise ValueError(msg)
        if any(
            type(value) is not bool
            for value in (self.truncated, self.environment_ready, self.baseline_reproduced)
        ):
            msg = "Investigation flags must be booleans"
            raise ValueError(msg)
        if self.environment_ready and (
            self.kind != "environment" or self.failure != FailureKind.NONE
        ):
            msg = "Only environment measurements establish readiness"
            raise ValueError(msg)
        if self.baseline_reproduced and (
            self.kind != "baseline"
            or self.failure not in {FailureKind.NONE, FailureKind.VERIFICATION}
        ):
            msg = "Only baseline execution establishes reproduction"
            raise ValueError(msg)
        if (self.assignment is not None) != (self.kind == "assignment"):
            msg = "Only validated assignment records may bind worker preparation"
            raise ValueError(msg)
