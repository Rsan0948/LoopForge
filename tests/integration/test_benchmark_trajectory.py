"""Real-stream pins for the M4 trajectory-quality metrics (PACS-016).

``compute_trajectory_metrics`` runs against REAL authoritative streams
produced by the locked fixtures through the full runtime (same gating
precedent as ``test_benchmark_graders.py``: real fixture commands, skip with
reason codes where the platform rejects the launcher's RLIMIT_AS
configuration, network-free):

- the scripted successful repair (bench-simple-bug): two model turns, no
  repetition, no violations, peak billed prompt of 100 tokens;
- the ambiguous-success naive run (bench-ambiguous-success under a scripted
  model repeating the known-wrong patch): the M2 fixture NATURALLY repeats
  one identical write_file proposal four times, so the repetition_ratio pin
  is 3/4 — the stall fixture's scripted behavior is the honest repetition
  signal, not a contrived stream;
- the accounting-precedence pin: the runtime's real ``ContextAccounting``
  ledger (captured through the ``ContextAccountingSource`` seam) beats the
  billed-usage fallback, proving the two-source contract on real data.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import FixedClock, RecordingSleeper, ScriptedModel
from loopforge.application.trajectory import (
    compute_trajectory_metrics,
    count_human_interventions,
)
from loopforge.domain.actions import ActionProposal
from loopforge.domain.benchmarks import TrajectoryMetrics
from loopforge.domain.events import Event
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunStatus
from loopforge.entrypoints.repair import (
    RepairRuntimeBundle,
    RepairRuntimeDeps,
    build_trusted_repair_runtime,
)
from loopforge.ports.context import ContextAccountingSource
from loopforge.ports.model import ModelPort
from loopforge.workloads.benchmarks import (
    AMBIGUOUS_NAIVE_SOLUTION,
    BenchmarkTaskBinding,
    build_benchmark_binding,
)
from loopforge.workloads.repair import RepairTask

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)


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


_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git executable unavailable; benchmark end-to-end tests require the Git CLI",
)
_REQUIRES_RLIMIT_AS = pytest.mark.skipif(
    not _rlimit_as_supported(),
    reason=(
        "platform rejects setrlimit(RLIMIT_AS); local sandbox launcher cannot apply "
        "resource limits, so command execution fails closed"
    ),
)

pytestmark = _REQUIRES_GIT


def _run(
    binding: BenchmarkTaskBinding, tmp_path: Path, model: ModelPort | None = None
) -> tuple[RunState, tuple[Event, ...], RepairRuntimeBundle]:
    """Drive one binding through the trusted runtime; return (state, events, bundle)."""
    task = binding.task
    assert isinstance(task, RepairTask)
    store = InMemoryEventStore()
    bundle = build_trusted_repair_runtime(
        task,
        workspaces_dir=tmp_path / "workspaces",
        deps=RepairRuntimeDeps(store=store, clock=CLOCK, sleeper=RecordingSleeper(), model=model),
        checks=binding.checks,
    )
    state = bundle.runtime.run(task.objective)
    return state, store.events_for(state.run_id), bundle


@_REQUIRES_RLIMIT_AS
def test_scripted_successful_repair_metrics(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-simple-bug")

    state, events, bundle = _run(binding, tmp_path)

    assert state.status is RunStatus.SUCCEEDED
    # The real stream honestly shows: two scripted turns (read + write), no
    # repeated proposal, every path inside the locked "slugs.py" scope, no
    # approvals, no recovery events, and a flat 100-token billed prompt per
    # turn (the scripted model's honest UsageDelta) as the fallback peak.
    assert compute_trajectory_metrics(binding.spec, events) == TrajectoryMetrics(
        model_turns=2,
        repetition_ratio=0.0,
        expensive_model_turns=0,
        scope_violations=0,
        permission_requests=0,
        context_tokens_used=100,
        context_items_dropped=0,
        recovery_events=0,
    )
    assert count_human_interventions(events) == 0
    bundle.close()


@_REQUIRES_RLIMIT_AS
def test_scripted_success_metrics_with_expensive_set(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-simple-bug")

    state, events, bundle = _run(binding, tmp_path)

    assert state.status is RunStatus.SUCCEEDED
    # The scripted model's code-owned identity is scripted/scripted-deterministic;
    # declaring it expensive marks every recorded turn, and only those turns.
    metrics = compute_trajectory_metrics(
        binding.spec,
        events,
        expensive_models=frozenset({("scripted", "scripted-deterministic")}),
    )
    assert metrics.expensive_model_turns == 2
    assert metrics.model_turns == 2
    bundle.close()


@_REQUIRES_RLIMIT_AS
def test_ambiguous_success_naive_stall_metrics_pin_repetition(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-ambiguous-success")
    model = ScriptedModel(
        [
            ActionProposal(
                ActionId(f"naive-{index}"),
                "write_file",
                {
                    "path": AMBIGUOUS_NAIVE_SOLUTION[0].path,
                    "content": AMBIGUOUS_NAIVE_SOLUTION[0].content,
                },
            )
            for index in range(1, 5)
        ]
    )

    state, events, bundle = _run(binding, tmp_path, model)

    assert state.status is RunStatus.STALLED
    # The stall fixture's scripted model repeats ONE identical write_file
    # proposal four times: 3 exact duplicates of 4 proposals -> 0.75. The
    # naive patch stays inside the locked "median.py" scope (0 violations),
    # the tool executions succeed (0 recovery events — VerificationFailed is
    # not a recovery event), and nobody intervenes.
    assert compute_trajectory_metrics(binding.spec, events) == TrajectoryMetrics(
        model_turns=4,
        repetition_ratio=0.75,
        expensive_model_turns=0,
        scope_violations=0,
        permission_requests=0,
        context_tokens_used=100,
        context_items_dropped=0,
        recovery_events=0,
    )
    assert count_human_interventions(events) == 0
    bundle.close()


@_REQUIRES_RLIMIT_AS
def test_accounting_precedence_on_the_real_ledger(tmp_path: Path) -> None:
    binding = build_benchmark_binding("bench-simple-bug")

    state, events, bundle = _run(binding, tmp_path)

    assert state.status is RunStatus.SUCCEEDED
    builder = bundle.runtime.context
    assert isinstance(builder, ContextAccountingSource)
    accounting = builder.last_accounting
    assert accounting is not None
    metrics = compute_trajectory_metrics(binding.spec, events, context_accountings=(accounting,))
    # The real ledger beats the billed-usage fallback: used_tokens comes from
    # the runtime's budgeting counter, not from UsageDelta.input_tokens.
    assert metrics.context_tokens_used == accounting.used_tokens
    assert metrics.context_tokens_used != 100
    assert metrics.context_items_dropped == len(accounting.dropped_entries) + sum(
        1 for entry in accounting.kept_entries if entry.compacted
    )
    bundle.close()
