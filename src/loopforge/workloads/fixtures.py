"""Deterministic fixture repositories for the software-repair workload.

Fixtures are code-owned and constructible fully offline (no network, no
external toolchains beyond the Python interpreter under test). Fixture file
*content* becomes untrusted repository content once materialized (AGENTS.md
rule 16); the definitions here are bootstrap authority.
"""

from __future__ import annotations

import sys

from loopforge.domain.types import WorkerId, WorkspaceId
from loopforge.domain.workspace import (
    AcceptanceCriteria,
    FixtureFile,
    FixtureSpec,
    PatchConstraints,
)
from loopforge.workloads.repair import (
    OrchestratedRepairTask,
    RepairCommand,
    RepairCommandKind,
    RepairTask,
    WorkerRepairAssignment,
)

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
        objective=(
            "Repair the adder regression so the fixture test suite passes. The buggy "
            "implementation is in adder.py at the workspace root and the unittest suite "
            "lives in tests/; only adder.py may be changed."
        ),
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


_GREETER_BUGGY = '''"""Tiny greeting module for the deterministic repair fixture."""


def greet(name: str) -> str:
    return f"Goodbye, {name}!"
'''

_GREETER_FIXED = '''"""Tiny greeting module for the deterministic repair fixture."""


def greet(name: str) -> str:
    return f"Hello, {name}!"
'''

_GREETER_TESTS = """import unittest

from greeter import greet


class GreeterTests(unittest.TestCase):
    def test_greets_by_name(self) -> None:
        self.assertEqual(greet("Ada"), "Hello, Ada!")

    def test_greeting_starts_with_hello(self) -> None:
        self.assertTrue(greet("Grace").startswith("Hello"))


if __name__ == "__main__":
    unittest.main()
"""


def calculator_fixture() -> FixtureSpec:
    """Two independent buggy modules for the orchestrated repair path (PACS-013).

    The modules are deliberately disjoint: one worker can repair ``adder.py``
    while another repairs ``greeter.py`` without sharing a mutable filesystem
    workspace, and their patches merge without conflict.
    """
    return FixtureSpec(
        fixture_id="calculator-regression",
        files=(
            FixtureFile(path="adder.py", content=_ADDER_BUGGY),
            FixtureFile(path="greeter.py", content=_GREETER_BUGGY),
            FixtureFile(path="tests/__init__.py", content=""),
            FixtureFile(path="tests/test_adder.py", content=_ADDER_TESTS),
            FixtureFile(path="tests/test_greeter.py", content=_GREETER_TESTS),
            FixtureFile(path=".gitignore", content=_GITIGNORE),
        ),
        solution=(
            FixtureFile(path="adder.py", content=_ADDER_FIXED),
            FixtureFile(path="greeter.py", content=_GREETER_FIXED),
        ),
    )


def _module_test_command(executable: str, module: str) -> RepairCommand:
    return RepairCommand(
        kind=RepairCommandKind.TEST,
        name="run_tests",
        # -B: never write bytecode caches; same-second repairs must not be
        # masked by a stale .pyc from the previous verification run.
        argv=(executable, "-B", "-m", "unittest", f"tests.test_{module}"),
        timeout_seconds=30.0,
        cpu_seconds=30,
    )


def _worker_repair_task(
    fixture: FixtureSpec,
    *,
    module: str,
    solution: FixtureFile,
    executable: str,
) -> RepairTask:
    """A worker-scoped repair task: its own suite and patch constraints only."""
    scoped_fixture = FixtureSpec(
        fixture_id=fixture.fixture_id,
        files=fixture.files,
        solution=(solution,),
    )
    return RepairTask(
        task_id=f"{fixture.fixture_id}-{module}",
        objective=(
            f"Repair the {module} regression so its fixture test suite passes. The buggy "
            f"implementation is in {module}.py at the workspace root and its unittest "
            f"suite lives in tests/test_{module}.py; only {module}.py may be changed."
        ),
        fixture=scoped_fixture,
        commands=(
            _module_test_command(executable, module),
            RepairCommand(
                kind=RepairCommandKind.BUILD,
                name="build",
                argv=(executable, "-B", "-m", "compileall", "-q", "-f", f"{module}.py"),
                timeout_seconds=30.0,
                cpu_seconds=30,
            ),
        ),
        acceptance=AcceptanceCriteria(
            required_commands=("run_tests",),
            patch=PatchConstraints(
                require_change=True,
                allowed_prefixes=(f"{module}.py",),
                max_changed_files=1,
            ),
        ),
    )


def calculator_repair_task(*, executable: str | None = None) -> OrchestratedRepairTask:
    """The canonical decomposed repair task over :func:`calculator_fixture`.

    Two workers with disjoint patch constraints repair one module each; the
    orchestrator merges their verified patches in spawn order and the
    integration acceptance gates success on the full suite. ``executable`` is
    code-owned wiring (``sys.executable`` locally, the in-container
    interpreter path for container sandboxes), never repository content.
    """
    resolved = executable or sys.executable
    fixture = calculator_fixture()
    return OrchestratedRepairTask(
        task_id="fixture-calculator-regression",
        objective=(
            "Repair the calculator regression so the full fixture test suite passes. "
            "The buggy implementations are adder.py and greeter.py at the workspace "
            "root and the unittest suites live in tests/; only those two modules may "
            "be changed."
        ),
        plan=(
            "worker adder repairs adder.py; worker greeter repairs greeter.py; merge "
            "worker branches in spawn order; verify the merged workspace against the "
            "full test suite."
        ),
        fixture=fixture,
        assignments=(
            WorkerRepairAssignment(
                worker_id=WorkerId("adder"),
                workspace_id=WorkspaceId("calculator-adder"),
                task=_worker_repair_task(
                    fixture,
                    module="adder",
                    solution=FixtureFile(path="adder.py", content=_ADDER_FIXED),
                    executable=resolved,
                ),
            ),
            WorkerRepairAssignment(
                worker_id=WorkerId("greeter"),
                workspace_id=WorkspaceId("calculator-greeter"),
                task=_worker_repair_task(
                    fixture,
                    module="greeter",
                    solution=FixtureFile(path="greeter.py", content=_GREETER_FIXED),
                    executable=resolved,
                ),
            ),
        ),
        commands=(
            RepairCommand(
                kind=RepairCommandKind.TEST,
                name="run_tests",
                argv=(resolved, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."),
                timeout_seconds=30.0,
                cpu_seconds=30,
            ),
            RepairCommand(
                kind=RepairCommandKind.BUILD,
                name="build",
                argv=(resolved, "-B", "-m", "compileall", "-q", "-f", "adder.py", "greeter.py"),
                timeout_seconds=30.0,
                cpu_seconds=30,
            ),
        ),
        acceptance=AcceptanceCriteria(
            required_commands=("run_tests",),
            # require_change stays off here by design: worker patches land as
            # merge *commits*, so the merged worktree is clean and status-based
            # change detection would see nothing. Each worker's own acceptance
            # enforced require_change within its disjoint prefix pre-merge; the
            # integration gate is the full suite passing on the merged tree,
            # plus defense-in-depth rejection of stray uncommitted edits.
            patch=PatchConstraints(
                require_change=False,
                allowed_prefixes=("adder.py", "greeter.py"),
                max_changed_files=2,
            ),
        ),
    )
