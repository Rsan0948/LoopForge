from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from loopforge.domain.types import ActionId


class _Arguments(Mapping[str, str]):
    """Detached immutable values; JSON arguments are strings, never nested objects."""

    __slots__ = ("_pairs",)
    _pairs: tuple[tuple[str, str], ...]

    def __init__(self, pairs: tuple[tuple[str, str], ...]) -> None:
        object.__setattr__(self, "_pairs", pairs)

    def __setattr__(self, name: str, value: object) -> None:
        msg = "action arguments are immutable"
        raise TypeError(msg)

    def __delattr__(self, name: str) -> None:
        msg = "action arguments are immutable"
        raise TypeError(msg)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._pairs)

    def __len__(self) -> int:
        return len(self._pairs)

    def __getitem__(self, key: str) -> str:
        for name, value in self._pairs:
            if name == key:
                return value
        raise KeyError(key)

    def __repr__(self) -> str:
        return repr(dict(self._pairs))

    def __deepcopy__(self, memo: dict[int, object]) -> _Arguments:
        # Copies retain immutability; the codec lowers mappings to JSON objects.
        return self


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """A model-requested action.

    Security and reliability semantics are intentionally absent: risk, permission,
    retry, idempotency, approval, and timeout policy belong to the registered tool
    contract, not to model output.
    """

    action_id: ActionId
    tool_name: str
    arguments: Mapping[str, str]
    expected_observation: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "proposal arguments must be a mapping"
            raise TypeError(msg)
        pairs = tuple(self.arguments.items())
        if any(
            not isinstance(key, str) or not isinstance(value, str)  # pyright: ignore[reportUnnecessaryIsInstance]
            for key, value in pairs
        ) or len({key for key, _ in pairs}) != len(pairs):
            msg = "proposal arguments must be an object of unique string keys and values"
            raise TypeError(msg)
        object.__setattr__(self, "arguments", _Arguments(pairs))
        if not self.tool_name.strip():
            msg = "tool_name cannot be empty"
            raise ValueError(msg)
