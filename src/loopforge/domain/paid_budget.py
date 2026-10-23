"""One immutable all-role cash envelope; unknown outcomes remain reserved."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class PaidAccount:
    ceiling: int
    spent: int = 0
    keys: tuple[str, ...] = ()
    pending: tuple[tuple[str, int, str], ...] = ()


@dataclass(frozen=True)
class PaidReserved:
    key: str
    maximum: int
    role: str


@dataclass(frozen=True)
class PaidSettled:
    key: str
    charged: int


def reduce_paid(state: PaidAccount, event: PaidReserved | PaidSettled) -> PaidAccount:
    if isinstance(event, PaidReserved):
        if (
            not event.key
            or event.key in state.keys
            or event.role not in {"supervisor", "review", "repair", "historical"}
            or type(event.maximum) is not int
            or event.maximum <= 0
            or state.spent + sum(p[1] for p in state.pending) + event.maximum > state.ceiling
        ):
            msg = "Parent paid budget reservation denied"
            raise ValueError(msg)
        return replace(
            state,
            keys=(*state.keys, event.key),
            pending=(*state.pending, (event.key, event.maximum, event.role)),
        )
    maximum = next((p[1] for p in state.pending if p[0] == event.key), None)
    if maximum is None or type(event.charged) is not int or not 0 <= event.charged <= maximum:
        msg = "Invalid paid settlement"
        raise ValueError(msg)
    return replace(
        state,
        spent=state.spent + event.charged,
        pending=tuple(p for p in state.pending if p[0] != event.key),
    )
