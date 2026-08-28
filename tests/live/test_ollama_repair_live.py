"""Live model tests — separated from deterministic CI.

Everything in this directory requires real external services (a running Ollama
server with the pinned model, a live Docker daemon) and skips with explicit
reason codes when they are unavailable, so deterministic CI remains runnable
with zero provider credentials and zero live infrastructure. Nondeterministic
model behavior must never leak into the deterministic suites.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.adapters.telemetry import InMemoryTelemetry
from loopforge.domain.events import ArtifactRecorded, BudgetDebited
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.types import RunStatus
from loopforge.entrypoints.repair import RepairRuntimeDeps, build_container_repair_runtime
from loopforge.workloads.fixtures import adder_repair_task
from loopforge.workloads.repair import repair_tool_specs

_OLLAMA_URL = "http://localhost:11434"
_OLLAMA_MODEL = "devstral-small-2:latest"
_TEST_IMAGE = "python:3.12-alpine"


class _TagEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class _TagsProbe(BaseModel):
    models: list[_TagEntry] = []


def _ollama_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{_OLLAMA_URL}/api/tags", timeout=5) as response:
            tags = _TagsProbe.model_validate(json.load(response))
    except (OSError, ValueError):
        return False
    return any(item.name == _OLLAMA_MODEL for item in tags.models)


def _image_available() -> bool:
    for reference in (_TEST_IMAGE, f"docker.io/library/{_TEST_IMAGE}"):
        try:
            inspect = subprocess.run(
                ["docker", "image", "inspect", reference],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if inspect.returncode == 0:
            return True
    return False


def _container_ready() -> bool:
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and _image_available()


_REQUIRES_OLLAMA = pytest.mark.skipif(
    not _ollama_ready(),
    reason=(
        f"ollama server or {_OLLAMA_MODEL} model unavailable; start Ollama and run "
        f"`ollama pull {_OLLAMA_MODEL}` to execute the live model repair run"
    ),
)

_REQUIRES_CONTAINER = pytest.mark.skipif(
    not _container_ready(),
    reason=(
        f"docker daemon or {_TEST_IMAGE} test image unavailable; start Docker and run "
        f"`docker pull {_TEST_IMAGE}` to execute the live model repair run"
    ),
)


@_REQUIRES_OLLAMA
@_REQUIRES_CONTAINER
def test_live_ollama_model_repairs_fixture_through_full_runtime(tmp_path: Path) -> None:
    """Acceptance gate: a live model completes the PACS-010 repair workload.

    The live adapter receives only budgeted ``ModelContext`` plus the
    versioned prompt contract; success is granted solely by the
    provider-independent verifier stack, and usage is debited from real
    provider token counts.
    """
    task = adder_repair_task(executable="/usr/local/bin/python")
    model = OllamaModel(
        model=_OLLAMA_MODEL,
        tools=repair_tool_specs(task),
        template=default_controller_template(),
        base_url=_OLLAMA_URL,
    )
    store = InMemoryEventStore()
    deps = RepairRuntimeDeps(
        store=store,
        clock=SystemClock(),
        sleeper=SystemSleeper(),
        telemetry=InMemoryTelemetry(),
        model=model,
    )
    bundle = build_container_repair_runtime(
        task,
        image=_TEST_IMAGE,
        workspaces_dir=tmp_path,
        deps=deps,
    )
    try:
        state = bundle.runtime.run(task.objective)
    finally:
        bundle.close()

    assert state.status is RunStatus.SUCCEEDED
    assert state.last_verification is not None
    assert "command:run_tests: passed (exit_code=0)" in state.last_verification

    events = store.events_for(state.run_id)
    debits = [event for event in events if isinstance(event, BudgetDebited)]
    assert debits, "live turns must debit real provider usage"
    assert all(debit.usage.input_tokens > 0 for debit in debits)
    assert all(debit.usage.output_tokens > 0 for debit in debits)

    artifacts = [event for event in events if isinstance(event, ArtifactRecorded)]
    assert artifacts, "the exact patch must be recorded as durable evidence"
    assert "+    return left + right" in artifacts[-1].content
