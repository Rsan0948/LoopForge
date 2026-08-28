"""Deterministic fixture repositories for the software-repair workload.

Fixtures are code-owned and constructible fully offline (no network, no
external toolchains beyond the Python interpreter under test). Fixture file
*content* becomes untrusted repository content once materialized (AGENTS.md
rule 16); the definitions here are bootstrap authority.
"""

from __future__ import annotations

import sys

from loopforge.domain.workspace import (
    AcceptanceCriteria,
    FixtureFile,
    FixtureSpec,
    PatchConstraints,
)
from loopforge.workloads.repair import RepairCommand, RepairCommandKind, RepairTask

_ADDER_BUGGY = '''"""Tiny arithmetic module for the deterministic repair fixture."""


def add(left: int, right: int) -> int:
    return left - right
'''

_ADDER_FIXED = '''"""Tiny arithmetic module for the deterministic repair fixture."""


def add(left: int, right: int) -> int:
    return left + right
'''

_ADDER_TESTS = """import unittest

from adder import add


class AdderTests(unittest.TestCase):
    def test_adds_positive_numbers(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_handles_negatives(self) -> None:
        self.assertEqual(add(-1, 1), 0)

    def test_addition_is_commutative(self) -> None:
        self.assertEqual(add(3, 4), add(4, 3))


if __name__ == "__main__":
    unittest.main()
"""

_GITIGNORE = "__pycache__/\n"


def adder_fixture() -> FixtureSpec:
    """A one-file arithmetic regression with a stdlib-only test suite."""
    return FixtureSpec(
        fixture_id="adder-regression",
        files=(
            FixtureFile(path="adder.py", content=_ADDER_BUGGY),
            FixtureFile(path="tests/__init__.py", content=""),
            FixtureFile(path="tests/test_adder.py", content=_ADDER_TESTS),
            FixtureFile(path=".gitignore", content=_GITIGNORE),
        ),
        solution=(FixtureFile(path="adder.py", content=_ADDER_FIXED),),
    )


def adder_repair_task(*, executable: str | None = None) -> RepairTask:
    """The canonical deterministic repair task over :func:`adder_fixture`.

    ``executable`` is the absolute Python interpreter path used for the
    predefined commands — ``sys.executable`` locally, or the in-container
    interpreter path when bound to a container sandbox. It is code-owned
    wiring, never repository or model content.
    """
    resolved = executable or sys.executable
    return RepairTask(
        task_id="fixture-adder-regression",
        objective="Repair the adder regression so the fixture test suite passes.",
        fixture=adder_fixture(),
        commands=(
            RepairCommand(
                kind=RepairCommandKind.TEST,
                name="run_tests",
                # -B: never write bytecode caches; same-second repairs must not
                # be masked by a stale .pyc from the previous verification run.
                argv=(resolved, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."),
                timeout_seconds=30.0,
                cpu_seconds=30,
            ),
            RepairCommand(
                kind=RepairCommandKind.BUILD,
                name="build",
                argv=(resolved, "-B", "-m", "compileall", "-q", "-f", "adder.py"),
                timeout_seconds=30.0,
                cpu_seconds=30,
            ),
        ),
        acceptance=AcceptanceCriteria(
            required_commands=("run_tests",),
            patch=PatchConstraints(
                require_change=True,
                allowed_prefixes=("adder.py",),
                max_changed_files=1,
            ),
        ),
    )
