"""Code-owned repair escalation thresholds and durable reservation reducer."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RepairSample:
    failure: str
    patch: str


def escalation_due(samples: tuple[RepairSample, ...]) -> bool:
    """Three consecutive same failures, or two on the identical candidate."""
    if len(samples) < 2:
        return False
    last = samples[-1]
    if samples[-2] == last:
        return True
    return len(samples) >= 3 and all(s.failure == last.failure for s in samples[-3:])


def stall_escalation_due(turns: int) -> bool:
    """Offer repair help before the immutable eight-turn stopping boundary."""
    return turns >= 6


@dataclass(frozen=True)
class RepairAccount:
    ceiling: int
    spent: int = 0
    keys: tuple[str, ...] = ()
    pending: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class RepairReserved:
    key: str
    maximum: int


@dataclass(frozen=True)
class RepairSettled:
    key: str
    charged: int


def reduce_repair(state: RepairAccount, event: RepairReserved | RepairSettled) -> RepairAccount:
    if isinstance(event, RepairReserved):
        if (
            not event.key
            or event.key in state.keys
            or type(event.maximum) is not int
            or event.maximum <= 0
            or len(state.keys) >= 3
            or state.spent + event.maximum > state.ceiling
        ):
            msg = "Repair reservation denied: used failure, call cap or hard budget"
            raise ValueError(msg)
        return replace(
            state,
            spent=state.spent + event.maximum,
            keys=(*state.keys, event.key),
            pending=(*state.pending, (event.key, event.maximum)),
        )
    maximum = dict(state.pending).get(event.key)
    if maximum is None or type(event.charged) is not int or not 0 <= event.charged <= maximum:
        msg = "Repair settlement denied: missing reservation or invalid charge"
        raise ValueError(msg)
    return replace(
        state,
        spent=state.spent - maximum + event.charged,
        pending=tuple(p for p in state.pending if p[0] != event.key),
    )
