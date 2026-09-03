"""Unit tests for the locked benchmark fixture set (PACS-016, M2).

Pins suite assembly (12 tasks, every category exactly once, unique namespaced
ids, self-consistent lock hash), grader/sandbox/live-eligibility wiring, the
binding surface (allow+deny per AGENTS.md rule 10), approval and fault
wiring, and the literal pinned ``benchmark_content_lock()`` hash — any edit
to any locked fixture must fail loudly here.
"""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

import loopforge.workloads.benchmarks as benchmark_module
from loopforge.domain.benchmarks import (
    BenchmarkCategory,
    BenchmarkSandboxMode,
    GraderId,
    suite_lock_hash,
)
from loopforge.domain.reliability import RetrySettings
from loopforge.workloads.benchmarks import (
    AMBIGUOUS_NAIVE_SOLUTION,
    BENCHMARK_SUITE_VERSION,
    PROMPT_INJECTION_TEXT,
    BenchmarkFault,
    BenchmarkFaultKind,
    BenchmarkTaskBinding,
    ambiguous_success_edge_check,
    benchmark_bindings,
    benchmark_content_lock,
    benchmark_suite,
    build_benchmark_binding,
)
from loopforge.workloads.repair import OrchestratedRepairTask, RepairTask

# Pinned operator-visible content lock: computed from the locked fixtures, so
# ANY fixture content edit changes the hash and fails this test loudly.
# Re-pinned for the M9 adversarial-review fix: the canonical payload now also
# covers each acceptance-check hook's SOURCE (inspect.getsource), so editing a
# check body (e.g. unconditional ``passed=True``) relocks loudly instead of
# silently neutralizing the false-success category the hook guards.
_PINNED_CONTENT_LOCK = "9bc7fe19125b4946c2f1e10301b0e1b7de77a31b2ce81b2635bf57c00e969924"

_LIVE_ELIGIBLE_TASKS = {
    "bench-simple-bug",
    "bench-multi-file",
    "bench-misleading-failure",
    "bench-ambiguous-success",
    "bench-context-pollution",
    "bench-stale-state",
    "bench-stall",
    "bench-prompt-injection",
    "bench-hitl",
    "bench-parallel-work",
}
_CONTAINER_TASKS = _LIVE_ELIGIBLE_TASKS
_TRUSTED_LOCAL_TASKS = {"bench-transient-api", "bench-provider-outage"}
_GROUND_TRUTH_TASKS = {"bench-ambiguous-success", "bench-prompt-injection"}
_RECOVERY_TASKS = {"bench-transient-api", "bench-provider-outage", "bench-stall"}


def _path_allowed(path: str, allowed_prefixes: tuple[str, ...]) -> bool:
    return any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed_prefixes
    )


def test_suite_has_twelve_tasks_covering_every_category_exactly_once() -> None:
    suite = benchmark_suite()

    assert suite.version == BENCHMARK_SUITE_VERSION == "1.0.0"
    assert len(suite.tasks) == 12
    categories = [task.category for task in suite.tasks]
    assert sorted(categories) == sorted(BenchmarkCategory)


def test_task_and_fixture_ids_are_unique_and_namespaced() -> None:
    suite = benchmark_suite()

    task_ids = [task.task_id for task in suite.tasks]
    fixture_ids = [task.fixture_id for task in suite.tasks]
    assert len(set(task_ids)) == len(task_ids)
    assert len(set(fixture_ids)) == len(fixture_ids)
    for task in suite.tasks:
        assert task.task_id.startswith("bench-")
        assert task.fixture_id.startswith("bench-")


def test_suite_lock_is_self_consistent_and_order_independent() -> None:
    suite = benchmark_suite()

    assert suite.lock_hash == suite_lock_hash(suite.tasks)
    reversed_tasks = tuple(reversed(suite.tasks))
    assert suite_lock_hash(reversed_tasks) == suite.lock_hash


def test_every_task_names_at_least_the_repair_graders() -> None:
    for task in benchmark_suite().tasks:
        assert GraderId.VERIFIED_SUCCESS in task.grader_ids
        assert GraderId.SCOPE_DISCIPLINE in task.grader_ids


def test_ground_truth_graders_pin_ambiguous_and_injection_tasks() -> None:
    for task in benchmark_suite().tasks:
        has_ground_truth = GraderId.GROUND_TRUTH in task.grader_ids
        assert has_ground_truth == (task.task_id in _GROUND_TRUTH_TASKS)


def test_recovery_graders_pin_fault_injection_and_stall_tasks() -> None:
    for task in benchmark_suite().tasks:
        has_recovery = GraderId.RECOVERY in task.grader_ids
        assert has_recovery == (task.task_id in _RECOVERY_TASKS)


def test_sandbox_mode_and_live_eligibility_are_pinned() -> None:
    for task in benchmark_suite().tasks:
        if task.task_id in _CONTAINER_TASKS:
            assert task.sandbox_mode is BenchmarkSandboxMode.CONTAINER
            assert task.live_eligible is True
        else:
            assert task.task_id in _TRUSTED_LOCAL_TASKS
            assert task.sandbox_mode is BenchmarkSandboxMode.TRUSTED_LOCAL
            assert task.live_eligible is False
    # Adversarial content must never run trusted-local (AGENTS.md rule 15).
    injection = build_benchmark_binding("bench-prompt-injection")
    assert injection.spec.sandbox_mode is BenchmarkSandboxMode.CONTAINER


def test_build_benchmark_binding_resolves_every_locked_task() -> None:
    suite = benchmark_suite()
    for spec in suite.tasks:
        binding = build_benchmark_binding(spec.task_id)
        assert binding.spec == spec
        assert binding.task.task_id == spec.task_id
        assert binding.task.fixture.fixture_id == spec.fixture_id


def test_build_benchmark_binding_rejects_unknown_task_id() -> None:
    with pytest.raises(ValueError, match="unknown benchmark task_id"):
        build_benchmark_binding("bench-does-not-exist")
    with pytest.raises(ValueError, match="unknown benchmark task_id"):
        build_benchmark_binding("")


def test_every_binding_task_validates_against_its_spec() -> None:
    for binding in benchmark_bindings():
        # The binding contract itself enforces this at construction; pin it
        # explicitly so a weakened __post_init__ is loud here.
        assert binding.spec.task_id == binding.task.task_id
        assert tuple(binding.spec.allowed_prefixes) == tuple(
            binding.task.acceptance.patch.allowed_prefixes
        )
        assert binding.task.acceptance.required_commands, (
            "acceptance required_commands must be non-empty (no vacuous success)"
        )


def test_allowed_prefixes_mirror_patch_constraints_and_cover_solutions() -> None:
    for binding in benchmark_bindings():
        prefixes = tuple(binding.spec.allowed_prefixes)
        assert prefixes, "every benchmark task bounds its patch scope"
        fixtures = [binding.task.fixture]
        if isinstance(binding.task, OrchestratedRepairTask):
            fixtures.extend(assignment.task.fixture for assignment in binding.task.assignments)
        for fixture in fixtures:
            assert fixture.solution, "every fixture carries a deterministic solution"
            for item in fixture.solution:
                assert _path_allowed(item.path, prefixes), (
                    f"solution path {item.path} escapes allowed prefixes {prefixes}"
                )


def test_approval_wiring_is_hitl_only() -> None:
    for binding in benchmark_bindings():
        if binding.spec.category is BenchmarkCategory.HITL:
            assert binding.approval_required_for == ("write_file", "edit_file")
        else:
            assert binding.approval_required_for == ()


def test_transient_fault_count_stays_below_the_default_retry_streak() -> None:
    # The TRANSIENT_API binding recovers only because its injected failure
    # streak is strictly shorter than the runtime's default bounded retry
    # streak (RetrySettings.max_attempts): with a streak at or above the
    # default the injected failures would exhaust the retry budget and the
    # trial would stop FAILURE instead of recovering. Pin the relationship
    # so a change to EITHER side fails loudly instead of silently flipping
    # the category's regime.
    transient = build_benchmark_binding("bench-transient-api")
    assert transient.fault is not None
    assert transient.fault.transient_failure_count < RetrySettings().max_attempts


def test_fault_descriptors_are_data_only_and_targeted() -> None:
    transient = build_benchmark_binding("bench-transient-api")
    assert transient.fault == BenchmarkFault(
        kind=BenchmarkFaultKind.TRANSIENT_API_FAILURE,
        transient_failure_count=2,
    )
    outage = build_benchmark_binding("bench-provider-outage")
    assert outage.fault == BenchmarkFault(
        kind=BenchmarkFaultKind.PROVIDER_OUTAGE,
        transient_failure_count=0,
    )
    for binding in benchmark_bindings():
        if binding.spec.task_id not in _TRUSTED_LOCAL_TASKS:
            assert binding.fault is None


def test_fault_descriptor_validation() -> None:
    BenchmarkFault(kind=BenchmarkFaultKind.PROVIDER_OUTAGE)  # allow: count defaults to 0
    with pytest.raises(ValueError, match="cannot be negative"):
        BenchmarkFault(kind=BenchmarkFaultKind.TRANSIENT_API_FAILURE, transient_failure_count=-1)


def test_ambiguous_binding_carries_the_edge_check_hook_and_naive_constant() -> None:
    binding = build_benchmark_binding("bench-ambiguous-success")
    assert binding.checks == (ambiguous_success_edge_check,)
    naive_paths = [item.path for item in AMBIGUOUS_NAIVE_SOLUTION]
    assert naive_paths == ["median.py"]
    for other in benchmark_bindings():
        if other.spec.task_id != "bench-ambiguous-success":
            assert other.checks == ()


def test_prompt_injection_content_present_in_fixture_absent_from_solution() -> None:
    fixture = build_benchmark_binding("bench-prompt-injection").task.fixture
    untrusted_text = "\n".join(item.content for item in fixture.files)
    assert PROMPT_INJECTION_TEXT in untrusted_text
    assert "IGNORE PREVIOUS INSTRUCTIONS" in untrusted_text
    solution_text = "\n".join(item.content for item in fixture.solution)
    assert "IGNORE PREVIOUS INSTRUCTIONS" not in solution_text
    # The solution repairs the bug without touching the tests.
    assert [item.path for item in fixture.solution] == ["csvparse.py"]


def test_pollution_docs_are_bounded_and_deterministic() -> None:
    fixture = build_benchmark_binding("bench-context-pollution").task.fixture
    docs = [item for item in fixture.files if item.path.startswith("docs/")]
    assert len(docs) >= 3
    total = sum(len(item.content.encode("utf-8")) for item in docs)
    assert 100_000 <= total <= 300_000


def test_stale_state_artifacts_are_labeled_untrusted() -> None:
    fixture = build_benchmark_binding("bench-stale-state").task.fixture
    paths = {item.path: item.content for item in fixture.files}
    assert "STALE" in paths["notes/STATUS.md"]
    assert "must never be trusted over actual command outcomes" in paths["notes/STATUS.md"]
    assert '"stale": true' in paths["state/last_run.json"]
    assert [item.path for item in fixture.solution] == ["ledger.py"]


def test_binding_rejects_spec_task_mismatch() -> None:
    binding = build_benchmark_binding("bench-simple-bug")
    mismatched_spec = build_benchmark_binding("bench-stall").spec
    with pytest.raises(ValueError, match="task_id must match"):
        BenchmarkTaskBinding(spec=mismatched_spec, task=binding.task)


def test_binding_rejects_scope_mismatch_between_spec_and_task() -> None:
    binding = build_benchmark_binding("bench-simple-bug")
    widened = replace(binding.spec, allowed_prefixes=("slugs.py", "other.py"))
    with pytest.raises(ValueError, match="must mirror the task patch constraints"):
        BenchmarkTaskBinding(spec=widened, task=binding.task)


def test_binding_rejects_duplicate_or_blank_approval_entries() -> None:
    binding = build_benchmark_binding("bench-simple-bug")
    with pytest.raises(ValueError, match="must be unique"):
        BenchmarkTaskBinding(
            spec=binding.spec,
            task=binding.task,
            approval_required_for=("write_file", "write_file"),
        )
    with pytest.raises(ValueError, match="non-empty tool names"):
        BenchmarkTaskBinding(
            spec=binding.spec,
            task=binding.task,
            approval_required_for=("  ",),
        )


def test_binding_rejects_non_callable_checks() -> None:
    binding = build_benchmark_binding("bench-simple-bug")
    with pytest.raises(TypeError, match="code-owned callables"):
        BenchmarkTaskBinding(
            spec=binding.spec,
            task=binding.task,
            checks=("not-a-callable",),  # pyright: ignore[reportArgumentType]
        )


def test_content_lock_matches_pinned_literal() -> None:
    assert benchmark_content_lock() == _PINNED_CONTENT_LOCK


def test_content_lock_covers_check_source_not_just_identity() -> None:
    # Deny pin for the hook-body attack: the canonical payload must embed
    # each check's inspect.getsource text, so weakening a check body changes
    # benchmark_content_lock() even though "module.qualname" is unchanged.
    binding = build_benchmark_binding("bench-ambiguous-success")
    canonical = benchmark_module._canonical_binding(binding)  # pyright: ignore[reportPrivateUsage]
    check = ambiguous_success_edge_check
    assert canonical["checks"] == [
        {
            "name": f"{check.__module__}.{check.__qualname__}",
            "source": inspect.getsource(check),
        }
    ]


def test_content_lock_is_stable_across_calls_and_executable_wiring() -> None:
    assert benchmark_content_lock() == benchmark_content_lock()
    # The interpreter path is runtime wiring, not locked content: bindings
    # built for a container interpreter must not perturb the lock payload.
    container_bindings = benchmark_bindings(executable="/usr/local/bin/python")
    assert len(container_bindings) == 12
    assert benchmark_content_lock() == _PINNED_CONTENT_LOCK


def test_single_and_orchestrated_task_shapes_are_pinned() -> None:
    orchestrated = {"bench-parallel-work"}
    for binding in benchmark_bindings():
        if binding.spec.task_id in orchestrated:
            assert isinstance(binding.task, OrchestratedRepairTask)
            worker_ids = [str(a.worker_id) for a in binding.task.assignments]
            assert worker_ids == ["metrics", "formatting"]
            prefixes = [a.task.acceptance.patch.allowed_prefixes for a in binding.task.assignments]
            assert prefixes == [("metrics.py",), ("formatting.py",)]
        else:
            assert isinstance(binding.task, RepairTask)
