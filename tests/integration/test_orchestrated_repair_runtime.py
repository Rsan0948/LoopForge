"""End-to-end orchestrated (multi-worker) repair runs (PACS-013).

Two workers repair disjoint modules of the calculator fixture, each in its
own linked worktree with a static budget share; the orchestrator merges
their verified patches in spawn order and the integration verifier gates
success on the full merged suite. The trusted-local runs execute real
fixture commands through ``ConstrainedLocalSandbox`` (RLIMIT_AS-gated like
the single-runtime suite); the container run exercises the untrusted path
through ``ContainerSandbox`` (daemon/image-gated). The merge-conflict run
proves conflicting state updates are rejected explicitly and replayably.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.domain.events import (
    ArtifactRecorded,
    PlanCreated,
    RunStarted,
    RunStopped,
    VerificationPassed,
    WorkerMerged,
    WorkerSpawned,
    WorkerStopped,
)
from loopforge.domain.orchestration import MergeOutcome, WorkerOutcome
from loopforge.domain.state import RunState, replay
from loopforge.domain.types import RunStatus, StopReason, WorkerId, WorkspaceId
from loopforge.domain.workspace import (
    AcceptanceCriteria,
    FixtureFile,
    FixtureSpec,
    PatchConstraints,
)
from loopforge.entrypoints.orchestrated import build_orchestrated_repair_runtime
from loopforge.entrypoints.repair import RepairRuntimeDeps
from loopforge.workloads.fixtures import calculator_repair_task
from loopforge.workloads.repair import (
    OrchestratedRepairTask,
    RepairCommand,
    RepairCommandKind,
    RepairTask,
    WorkerRepairAssignment,
)

_TEST_IMAGE = "python:3.12-alpine"


def _rlimit_as_supported() -> bool:
    probe = "import resource; resource.setrlimit(resource.RLIMIT_AS, (268435456, 268435456))"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _image_available() -> bool:
    # Docker Desktop's containerd image store can fail short-name resolution in
    # `image inspect` ("No such image") while `docker run` works; the canonical
    # fully-qualified reference is the reliable probe.
    for reference in (_TEST_IMAGE, f"docker.io/library/{_TEST_IMAGE}"):
        try:
            image = subprocess.run(
                ["docker", "image", "inspect", reference],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if image.returncode == 0:
            return True
    return False


def _container_ready() -> bool:
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and _image_available()


_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git executable unavailable; orchestrated end-to-end tests require the Git CLI",
)
_REQUIRES_RLIMIT_AS = pytest.mark.skipif(
    not _rlimit_as_supported(),
    reason=(
        "platform rejects setrlimit(RLIMIT_AS); local sandbox launcher cannot apply "
        "resource limits, so command execution fails closed"
    ),
)
_REQUIRES_CONTAINER = pytest.mark.skipif(
    not _container_ready(),
    reason=(
        f"docker daemon or {_TEST_IMAGE} test image unavailable; start Docker and run "
        f"`docker pull {_TEST_IMAGE}` to execute the live container orchestrated run"
    ),
)

pytestmark = _REQUIRES_GIT


def _deps(store: InMemoryEventStore) -> RepairRuntimeDeps:
    return RepairRuntimeDeps(store=store, clock=SystemClock(), sleeper=SystemSleeper())


def _run_orchestrated(
    tmp_path: Path,
    *,
    container_image: str | None = None,
) -> tuple[InMemoryEventStore, RunState]:
    executable = "/usr/local/bin/python" if container_image is not None else None
    task = calculator_repair_task(executable=executable)
    store = InMemoryEventStore()
    bundle = build_orchestrated_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store),
        container_image=container_image,
    )
    try:
        state = bundle.orchestrator.run(task.objective, task.plan)
    finally:
        bundle.close()
    return store, state


def _assert_successful_orchestrated_run(store: InMemoryEventStore, state: RunState) -> None:
    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED
    workers = state.workers
    assert [str(worker.worker_id) for worker in workers] == ["adder", "greeter"]
    assert all(worker.outcome is WorkerOutcome.SUCCEEDED for worker in workers)
    assert all(worker.merge_outcome is MergeOutcome.MERGED for worker in workers)

    events = store.events_for(state.run_id)
    kinds = [type(event) for event in events]
    assert kinds[:2] == [RunStarted, PlanCreated]
    assert kinds.count(WorkerSpawned) == 2
    assert kinds.count(WorkerStopped) == 2
    assert kinds.count(WorkerMerged) == 2
    assert kinds.count(VerificationPassed) == 1
    assert kinds.count(ArtifactRecorded) == 1
    assert isinstance(events[-1], RunStopped)

    # Worker ownership is durable: spawn events name the worker run streams,
    # and those streams carry the per-worker verified patch evidence.
    spawned = [event for event in events if isinstance(event, WorkerSpawned)]
    for spawn in spawned:
        worker_events = store.events_for(spawn.worker_run_id)
        assert worker_events, f"no durable stream for worker {spawn.worker_id}"
        artifacts = [event for event in worker_events if isinstance(event, ArtifactRecorded)]
        assert artifacts, f"no workspace evidence for worker {spawn.worker_id}"
        changed_module = f"{spawn.worker_id}.py"
        assert f"changed_files={changed_module}" in artifacts[-1].content

    # The orchestrated stream replays to the exact terminal state.
    assert replay(state.run_id, events) == state


@_REQUIRES_RLIMIT_AS
def test_two_workers_repair_disjoint_modules_through_trusted_local_sandbox(
    tmp_path: Path,
) -> None:
    store, state = _run_orchestrated(tmp_path)
    _assert_successful_orchestrated_run(store, state)


@_REQUIRES_CONTAINER
def test_two_workers_repair_disjoint_modules_through_untrusted_container(
    tmp_path: Path,
) -> None:
    store, state = _run_orchestrated(tmp_path, container_image=_TEST_IMAGE)
    _assert_successful_orchestrated_run(store, state)


_SHARED_BUGGY = '''"""Shared module both workers repair differently."""


def combine(left: int, right: int) -> int:
    return left - right
'''

_SHARED_TESTS = """import unittest

from shared import combine


class SharedTests(unittest.TestCase):
    def test_combine_adds(self) -> None:
        self.assertEqual(combine(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
"""

_GITIGNORE = "__pycache__/\n*.pyc\n"


def _conflicting_task(executable: str) -> OrchestratedRepairTask:
    """Two workers both repair ``shared.py`` with different (both valid) fixes.

    Each worker succeeds in isolation; their branches touch the same line, so
    the spawn-order merge of the second worker conflicts by construction.
    """
    fixture = FixtureSpec(
        fixture_id="shared-conflict",
        files=(
            FixtureFile(path="shared.py", content=_SHARED_BUGGY),
            FixtureFile(path="tests/__init__.py", content=""),
            FixtureFile(path="tests/test_shared.py", content=_SHARED_TESTS),
            FixtureFile(path=".gitignore", content=_GITIGNORE),
        ),
        # The integration fixture's solution is unused (worker tasks carry
        # their own scoped views); keep it minimal but well-formed.
        solution=(FixtureFile(path="shared.py", content=_SHARED_BUGGY.replace("-", "+")),),
    )

    def worker_task(worker_id: str, expression: str) -> RepairTask:
        solution = FixtureFile(
            path="shared.py",
            content=_SHARED_BUGGY.replace("left - right", expression),
        )
        return RepairTask(
            task_id=f"shared-conflict-{worker_id}",
            objective=f"Repair the shared.py regression (worker {worker_id}).",
            fixture=FixtureSpec(
                fixture_id=fixture.fixture_id,
                files=fixture.files,
                solution=(solution,),
            ),
            commands=(
                RepairCommand(
                    kind=RepairCommandKind.TEST,
                    name="run_tests",
                    argv=(executable, "-B", "-m", "unittest", "tests.test_shared"),
                    timeout_seconds=30.0,
                    cpu_seconds=30,
                ),
            ),
            acceptance=AcceptanceCriteria(
                required_commands=("run_tests",),
                patch=PatchConstraints(
                    require_change=True,
                    allowed_prefixes=("shared.py",),
                    max_changed_files=1,
                ),
            ),
        )

    return OrchestratedRepairTask(
        task_id="shared-conflict-orchestrated",
        objective="Repair the shared.py regression with two workers.",
        plan="both workers repair shared.py; merge in spawn order.",
        fixture=fixture,
        assignments=(
            WorkerRepairAssignment(
                worker_id=WorkerId("first"),
                workspace_id=WorkspaceId("shared-first"),
                task=worker_task("first", "left + right"),
            ),
            WorkerRepairAssignment(
                worker_id=WorkerId("second"),
                workspace_id=WorkspaceId("shared-second"),
                task=worker_task("second", "right + left"),
            ),
        ),
        commands=(
            RepairCommand(
                kind=RepairCommandKind.TEST,
                name="run_tests",
                argv=(executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."),
                timeout_seconds=30.0,
                cpu_seconds=30,
            ),
        ),
        acceptance=AcceptanceCriteria(
            required_commands=("run_tests",),
            patch=PatchConstraints(require_change=False, allowed_prefixes=("shared.py",)),
        ),
    )


@_REQUIRES_RLIMIT_AS
def test_conflicting_worker_patches_stop_the_run_explicitly(tmp_path: Path) -> None:
    task = _conflicting_task(sys.executable)
    store = InMemoryEventStore()
    bundle = build_orchestrated_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store),
    )
    try:
        state = bundle.orchestrator.run(task.objective, task.plan)
    finally:
        bundle.close()

    # Both workers repaired the module in isolation; reconciliation rejected
    # the conflicting second branch explicitly — never a silent overwrite.
    assert state.status is RunStatus.FAILED
    assert state.stop_reason is StopReason.FAILURE
    events = store.events_for(state.run_id)
    merges = [event for event in events if isinstance(event, WorkerMerged)]
    assert [merge.outcome for merge in merges] == [MergeOutcome.MERGED, MergeOutcome.CONFLICT]
    assert merges[0].revision is not None
    assert merges[1].revision is None
    stop = events[-1]
    assert isinstance(stop, RunStopped)
    assert "WORKER_MERGE_CONFLICT" in stop.summary
    assert replay(state.run_id, events) == state
