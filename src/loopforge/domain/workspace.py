from __future__ import annotations

from dataclasses import dataclass, field

_GIT_DIR_NAME = ".git"
_MAX_PATH_LENGTH = 512
_MAX_NAME_LENGTH = 128


def _has_unsafe_characters(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _validate_relative_path(path: str, *, field_name: str) -> None:
    if not path.strip():
        msg = f"{field_name} cannot be empty"
        raise ValueError(msg)
    if len(path) > _MAX_PATH_LENGTH:
        msg_5 = f"{field_name} cannot exceed {_MAX_PATH_LENGTH} characters"
        raise ValueError(msg_5)
    if "\\" in path or _has_unsafe_characters(path):
        msg_4 = f"{field_name} must not contain backslashes or control characters"
        raise ValueError(msg_4)
    parts = path.split("/")
    if path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        msg_2 = f"{field_name} must be a relative path without '.', '..', or empty segments"
        raise ValueError(msg_2)
    if parts[0] == _GIT_DIR_NAME:
        # Repository metadata is never legitimate workload content: a writable
        # .git would let fixture/model content steer host-side Git execution.
        msg_3 = f"{field_name} cannot target the Git metadata directory"
        raise ValueError(msg_3)


def _validate_name(name: str, *, field_name: str) -> None:
    if not name.strip():
        msg = f"{field_name} cannot be empty"
        raise ValueError(msg)
    if any(char in name for char in "/\\") or name in {".", ".."}:
        msg_2 = f"{field_name} must be a plain name without path separators"
        raise ValueError(msg_2)
    if len(name) > _MAX_NAME_LENGTH or _has_unsafe_characters(name):
        msg_3 = f"{field_name} must not contain control characters or exceed 128 characters"
        raise ValueError(msg_3)


@dataclass(frozen=True, slots=True, kw_only=True)
class FixtureFile:
    """One code-owned file of a deterministic fixture repository."""

    path: str
    content: str

    def __post_init__(self) -> None:
        _validate_relative_path(self.path, field_name="fixture file path")


@dataclass(frozen=True, slots=True, kw_only=True)
class FixtureSpec:
    """Code-owned, offline-constructible fixture repository definition.

    Fixture *content* is untrusted once materialized (AGENTS.md rule 16), but
    the fixture definition itself is bootstrap authority: repository content
    can never add files, widen acceptance criteria, or alter verification.
    """

    fixture_id: str
    files: tuple[FixtureFile, ...]
    solution: tuple[FixtureFile, ...] = ()

    def __post_init__(self) -> None:
        _validate_name(self.fixture_id, field_name="fixture_id")
        if not self.files:
            msg = "fixture must contain at least one file"
            raise ValueError(msg)
        paths = [item.path for item in self.files]
        if len(set(paths)) != len(paths):
            msg_2 = "fixture file paths must be unique"
            raise ValueError(msg_2)
        known = set(paths)
        solution_paths = [item.path for item in self.solution]
        if len(set(solution_paths)) != len(solution_paths):
            msg_3 = "fixture solution paths must be unique"
            raise ValueError(msg_3)
        for item in self.solution:
            if item.path not in known:
                msg_4 = f"fixture solution path {item.path!r} is not part of the fixture"
                raise ValueError(msg_4)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceStatus:
    """Deterministic snapshot of workspace changes against the base revision."""

    changed: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.changed and not self.untracked

    @property
    def files(self) -> tuple[str, ...]:
        """All paths differing from the base revision, sorted and de-duplicated."""
        return tuple(sorted(set(self.changed) | set(self.untracked)))


@dataclass(frozen=True, slots=True, kw_only=True)
class PatchConstraints:
    """Code-owned constraints a repair patch must satisfy."""

    require_change: bool = True
    allowed_prefixes: tuple[str, ...] = ()
    max_changed_files: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.require_change, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = "require_change must be a bool"
            raise TypeError(msg_2)
        for prefix in self.allowed_prefixes:
            _validate_relative_path(prefix, field_name="allowed path prefix")
        if self.max_changed_files is not None and (
            isinstance(self.max_changed_files, bool)
            or not isinstance(self.max_changed_files, int)  # pyright: ignore[reportUnnecessaryIsInstance]
            or self.max_changed_files <= 0
        ):
            msg = "max_changed_files must be a positive int when set"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptanceCriteria:
    """Code-owned acceptance contract for a repair task.

    Repository or model content may never widen these criteria (AGENTS.md
    rules 14 and 16); success is granted only when every required command
    succeeds through the sandbox and the patch constraints hold.
    """

    required_commands: tuple[str, ...] = ()
    patch: PatchConstraints = field(default_factory=PatchConstraints)

    def __post_init__(self) -> None:
        names = list(self.required_commands)
        if any(not isinstance(name, str) or not name.strip() for name in names):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "required command names cannot be empty"
            raise ValueError(msg)
        if len(set(names)) != len(names):
            msg_2 = "required command names must be unique"
            raise ValueError(msg_2)
