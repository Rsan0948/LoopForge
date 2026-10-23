"""Resolve linked continuation envelopes without resetting the original paid cap."""

import json
from pathlib import Path

from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.domain.goals import GoalState


def paid_root(directory: Path) -> tuple[Path, GoalState]:
    directory = directory.resolve()
    root = directory.parent
    seen: set[Path] = set()
    for _ in range(32):
        if directory in seen:
            msg = "Cyclic paid-budget ancestry"
            raise ValueError(msg)
        seen.add(directory)
        state = SQLiteGoalJournal(directory / "goals.sqlite").load("prepared-goal")
        if state.policy is None:
            msg = "Missing parent paid policy"
            raise ValueError(msg)
        link = directory / "parent-link.json"
        if not link.exists():
            return directory, state
        parent = Path(json.loads(link.read_text())["directory"])
        if parent.is_symlink() or parent.resolve().parent != root:
            msg = "Parent paid ledger must belong to a sibling goal"
            raise ValueError(msg)
        parent_state = SQLiteGoalJournal(parent / "goals.sqlite").load("prepared-goal")
        if not (parent_state.paused or parent_state.terminal):
            msg = "Parent work must be reconciled"
            raise ValueError(msg)
        directory = parent.resolve()
    msg = "Paid-budget ancestry exceeds its bound"
    raise ValueError(msg)
