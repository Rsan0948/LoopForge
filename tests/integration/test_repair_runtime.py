"""End-to-end software-repair runs through the full runtime.

The local-sandbox end-to-end runs execute real fixture commands through
``ConstrainedLocalSandbox`` and are capability-gated exactly like the existing
local-adapter suites: platforms that reject the launcher's ``RLIMIT_AS``
configuration skip with reason codes. The container end-to-end run executes
the untrusted path through ``ContainerSandbox`` and is daemon/image-gated.
Everything else in the repair stack is covered daemon-free by unit suites.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import FixedClock, RecordingSleeper, ScriptedModel
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import (
    ArtifactRecorded,
    Event,
    ToolFailed,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.state import replay
from loopforge.domain.types import ActionId, RunStatus, StopReason
from loopforge.entrypoints.repair import (
    RepairRuntimeDeps,
    build_container_repair_runtime,
    build_trusted_repair_runtime,
)
from loopforge.ports.state_store import StateStorePort
from loopforge.workloads.fixtures import adder_repair_task
from loopforge.workloads.repair import RepairTask

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
_TEST_IMAGE = "python:3.12-alpine"

_WRONG_FIX = '''"""Tiny arithmetic module for the deterministic repair fixture."""


def add(left: int, right: int) -> int:
    return left * right
'''


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
    reason="git executable unavailable; repair end-to-end tests require the Git CLI",
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
        f"`docker pull {_TEST_IMAGE}` to execute the live container repair run"
    ),
)

pytestmark = _REQUIRES_GIT


def _artifacts(events: tuple[Event, ...]) -> list[ArtifactRecorded]:
    return [event for event in events if isinstance(event, ArtifactRecorded)]


def _deps(store: StateStorePort) -> RepairRuntimeDeps:
    return RepairRuntimeDeps(store=store, clock=CLOCK, sleeper=RecordingSleeper())


def _trusted_bundle(tmp_path: Path, task: RepairTask, store: InMemoryEventStore):
    return build_trusted_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store),
    )


@_REQUIRES_RLIMIT_AS
def test_scripted_model_repairs_fixture_through_full_runtime(tmp_path: Path) -> None:
    task = adder_repair_task()
    store = InMemoryEventStore()
    bundle = _trusted_bundle(tmp_path, task, store)

    state = bundle.runtime.run(task.objective)

    assert state.status is RunStatus.SUCCEEDED
    assert state.stop_reason is StopReason.SUCCESS_VERIFIED
    events = store.events_for(state.run_id)

    # Success is granted only by the independent verifier stack.
    passed = [event for event in events if isinstance(event, VerificationPassed)]
    assert len(passed) == 1
    assert "command:run_tests: passed (exit_code=0)" in passed[0].summary
    assert "patch_constraints: passed" in passed[0].summary

    # The replayable run captures the exact patch and verification evidence.
    artifacts = _artifacts(events)
    assert artifacts, "workspace snapshot evidence must be durable"
    latest = artifacts[-1]
    assert latest.kind.value == "workspace_snapshot"
    assert f"base_revision={bundle.workspace.base_revision}" in latest.content
    assert "changed_files=adder.py" in latest.content
    assert "-    return left - right" in latest.content
    assert "+    return left + right" in latest.content

    # The repair really landed, confined to the assigned workspace.
    repaired = (bundle.workspace.root / "adder.py").read_text(encoding="utf-8")
    assert repaired == task.fixture.solution[0].content
    assert bundle.workspace.status().files == ("adder.py",)

    # The durable stream round-trips through the strict codec and replays.
    codec = JsonEventCodec()
    decoded = tuple(codec.decode(codec.encode(event)) for event in events)
    assert decoded == events
    assert replay(state.run_id, decoded) == state


@_REQUIRES_RLIMIT_AS
def test_repair_run_is_durable_and_replayable_through_sqlite(tmp_path: Path) -> None:
    task = adder_repair_task()
    store = SQLiteEventStore(tmp_path / "events.db", codec=JsonEventCodec())
    bundle = build_trusted_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store),
    )

    state = bundle.runtime.run(task.objective)

    assert state.status is RunStatus.SUCCEEDED
    reopened = SQLiteEventStore(tmp_path / "events.db", codec=JsonEventCodec())
    events = reopened.events_for(state.run_id)
    assert replay(state.run_id, events) == state
    artifacts = _artifacts(events)
    assert "+    return left + right" in artifacts[-1].content


@_REQUIRES_RLIMIT_AS
def test_false_success_attempt_is_rejected(tmp_path: Path) -> None:
    task = adder_repair_task()
    store = InMemoryEventStore()
    bundle = _trusted_bundle(tmp_path, task, store)
    # The scripted model applies a wrong fix while *claiming* success via the
    # expected-observation channel; verifier truth must ignore the claim.
    bundle.runtime.model = ScriptedModel(
        [
            ActionProposal(
                ActionId(f"wrong-{index}"),
                "write_file",
                {"path": "adder.py", "content": _WRONG_FIX},
                expected_observation="all tests pass",
            )
            for index in range(1, 5)
        ]
    )

    state = bundle.runtime.run(task.objective)

    assert state.status is not RunStatus.SUCCEEDED
    assert state.status is RunStatus.STALLED
    events = store.events_for(state.run_id)
    assert not [event for event in events if isinstance(event, VerificationPassed)]
    failures = [event for event in events if isinstance(event, VerificationFailed)]
    assert failures
    assert all("command:run_tests: failed" in event.summary for event in failures)
    # Evidence still captures the exact (wrong) patch for replay/audit.
    artifacts = _artifacts(events)
    assert "return left * right" in artifacts[-1].content


@_REQUIRES_RLIMIT_AS
def test_unrepaired_fixture_cannot_pass_verification(tmp_path: Path) -> None:
    task = adder_repair_task()
    store = InMemoryEventStore()
    bundle = _trusted_bundle(tmp_path, task, store)
    bundle.runtime.model = ScriptedModel(
        [
            ActionProposal(
                ActionId(f"read-{index}"),
                "read_file",
                {"path": "adder.py"},
                expected_observation="all tests pass",
            )
            for index in range(1, 5)
        ]
    )

    state = bundle.runtime.run(task.objective)

    assert state.status is not RunStatus.SUCCEEDED
    assert not [
        event for event in store.events_for(state.run_id) if isinstance(event, VerificationPassed)
    ]


def test_workspace_changes_stay_confined_to_assigned_workspace(tmp_path: Path) -> None:
    task = adder_repair_task()
    store = InMemoryEventStore()
    bundle = _trusted_bundle(tmp_path, task, store)
    sentinel = tmp_path / "escape.py"
    bundle.runtime.model = ScriptedModel(
        [
            ActionProposal(
                ActionId("escape"),
                "write_file",
                {"path": "../escape.py", "content": "x = 1\n"},
            ),
            ActionProposal(
                ActionId("escape-absolute"),
                "write_file",
                {"path": "/tmp/loopforge-escape.py", "content": "x = 1\n"},
            ),
            ActionProposal(
                ActionId("read"),
                "read_file",
                {"path": "adder.py"},
            ),
            ActionProposal(
                ActionId("fix"),
                "write_file",
                {"path": "adder.py", "content": task.fixture.solution[0].content},
            ),
            # Trailing no-op turns let the run terminate through control policy
            # even where the local launcher fails closed (RLIMIT_AS platforms).
            *[
                ActionProposal(
                    ActionId(f"settle-{index}"),
                    "workspace_status",
                    {},
                )
                for index in range(1, 6)
            ],
        ]
    )

    state = bundle.runtime.run(task.objective)

    events = store.events_for(state.run_id)
    failures = [event for event in events if isinstance(event, ToolFailed)]
    policy_rejections = [event for event in failures if event.error_code == "SANDBOX_POLICY"]
    assert len(policy_rejections) == 2
    assert not sentinel.exists()
    assert not Path("/tmp/loopforge-escape.py").exists()
    assert set(bundle.workspace.status().files) <= {"adder.py"}


@_REQUIRES_CONTAINER
def test_live_container_repair_through_untrusted_boundary(tmp_path: Path) -> None:
    task = adder_repair_task(executable="/usr/local/bin/python")
    store = InMemoryEventStore()
    bundle = build_container_repair_runtime(
        task,
        image=_TEST_IMAGE,
        workspaces_dir=tmp_path / "workspaces",
        deps=_deps(store),
    )

    state = bundle.runtime.run(task.objective)

    assert state.status is RunStatus.SUCCEEDED
    events = store.events_for(state.run_id)
    passed = [event for event in events if isinstance(event, VerificationPassed)]
    assert len(passed) == 1
    artifacts = _artifacts(events)
    assert "+    return left + right" in artifacts[-1].content
    assert bundle.workspace.status().files == ("adder.py",)
