"""Pins for the deterministic benchmark graders (PACS-016, M3).

Every grader gets synthetic-stream allow+deny pairs (AGENTS.md rule 10):
verified-success pass/fail; scope pass/violation/false-success; ground-truth
pass/weakened-tests/deleted-tests/false-success/naive-patch/missing-hook-
evidence; recovery outage-graceful vs wedged, transient-recovered vs
unrecovered, stall-bounded vs budget-burning — plus the definitional pins for
``trial_is_success`` / ``trial_is_false_success`` (the M5 aggregation
contract). All streams are hand-built durable event tuples in the
``test_provenance.py`` style; no network, no sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from loopforge.application import graders
from loopforge.application.graders import (
    GraderEvidence,
    grade_trial,
    trial_is_false_success,
    trial_is_success,
)
from loopforge.domain.benchmarks import (
    BenchmarkCategory,
    BenchmarkSandboxMode,
    BenchmarkTaskSpec,
    GraderId,
    GraderResult,
    GraderVerdict,
)
from loopforge.domain.events import (
    Event,
    ModelTurnRecorded,
    RunStarted,
    RunStopped,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.types import ActionId, EventId, RunId, StopReason
from loopforge.domain.workspace import FixtureFile

NOW = datetime(2026, 9, 3, tzinfo=UTC)
RUN = RunId("grader-run")

_TEST_FILE = FixtureFile(path="tests/test_slugs.py", content="import unittest\n# the suite\n")
_SOURCE_FIXED = FixtureFile(
    path="slugs.py", content="def slugify(text):\n    return '-'.join(text.split())\n"
)
_NAIVE_FILE = FixtureFile(path="slugs.py", content="def slugify(text):\n    return text\n")
_PASS_SUMMARY = "command:run_tests: passed (exit_code=0)"
_HOOK_PASS_SUMMARY = "command:run_tests: passed (exit_code=0); edge_cases: passed (exit_code=0)"
_OUTAGE_SUMMARY = "MODEL_UNAVAILABLE: provider unavailable (HTTP 503)"


def _event_id(sequence: int) -> EventId:
    return EventId(f"e{sequence}")


def _started(sequence: int) -> RunStarted:
    return RunStarted(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        objective="repair the fixture",
    )


def _turn(sequence: int, action_id: str = "a1") -> ModelTurnRecorded:
    return ModelTurnRecorded(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        provider="stub",
        model="scripted",
        action_id=ActionId(action_id),
    )


def _passed(sequence: int, summary: str = _PASS_SUMMARY) -> VerificationPassed:
    return VerificationPassed(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        summary=summary,
    )


def _failed_verification(sequence: int, summary: str) -> VerificationFailed:
    return VerificationFailed(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        summary=summary,
    )


def _stop(sequence: int, reason: StopReason, summary: str = "stop") -> RunStopped:
    return RunStopped(
        event_id=_event_id(sequence),
        run_id=RUN,
        occurred_at=NOW,
        sequence=sequence,
        reason=reason,
        summary=summary,
    )


def _success_stream() -> tuple[Event, ...]:
    """A verifier-granted success: turn, passing verification, success stop."""
    return (
        _started(1),
        _turn(2),
        _passed(3),
        _stop(4, StopReason.SUCCESS_VERIFIED, "run completed"),
    )


def _spec(
    *,
    category: BenchmarkCategory = BenchmarkCategory.SIMPLE_BUG,
    grader_ids: tuple[GraderId, ...] = (GraderId.VERIFIED_SUCCESS, GraderId.SCOPE_DISCIPLINE),
    allowed_prefixes: tuple[str, ...] = ("slugs.py",),
) -> BenchmarkTaskSpec:
    return BenchmarkTaskSpec(
        task_id="bench-test",
        category=category,
        objective="repair the fixture so the tests pass",
        fixture_id="bench-test",
        sandbox_mode=BenchmarkSandboxMode.CONTAINER,
        grader_ids=grader_ids,
        allowed_prefixes=allowed_prefixes,
    )


def _evidence(  # noqa: PLR0913 - test wiring keeps every evidence field explicit
    *,
    final_changed_files: tuple[str, ...] = ("slugs.py",),
    test_files: tuple[FixtureFile, ...] = (_TEST_FILE,),
    expected_test_files: tuple[FixtureFile, ...] = (_TEST_FILE,),
    final_sources: tuple[FixtureFile, ...] = (_SOURCE_FIXED,),
    verification_summaries: tuple[str, ...] = (_PASS_SUMMARY,),
    deleted_test_files: tuple[str, ...] = (),
    required_check_names: tuple[str, ...] = (),
    naive_solution: tuple[FixtureFile, ...] = (),
) -> GraderEvidence:
    return GraderEvidence(
        final_changed_files=final_changed_files,
        test_files=test_files,
        expected_test_files=expected_test_files,
        final_sources=final_sources,
        verification_summaries=verification_summaries,
        deleted_test_files=deleted_test_files,
        required_check_names=required_check_names,
        naive_solution=naive_solution,
    )


def _grade(
    grader_id: GraderId,
    events: tuple[Event, ...],
    evidence: GraderEvidence,
    *,
    category: BenchmarkCategory = BenchmarkCategory.SIMPLE_BUG,
    allowed_prefixes: tuple[str, ...] = ("slugs.py",),
) -> GraderResult:
    spec = _spec(category=category, grader_ids=(grader_id,), allowed_prefixes=allowed_prefixes)
    (result,) = grade_trial(spec, events, evidence)
    return result


# --- GraderEvidence validation (house-style strictness) -------------------------


def test_evidence_rejects_deleted_paths_that_also_survive() -> None:
    with pytest.raises(ValueError, match="overlap surviving test files"):
        _evidence(deleted_test_files=(_TEST_FILE.path,))


def test_evidence_rejects_duplicate_file_paths() -> None:
    with pytest.raises(ValueError, match="expected_test_files paths must be unique"):
        _evidence(expected_test_files=(_TEST_FILE, _TEST_FILE))


def test_evidence_rejects_non_fixture_file_entries() -> None:
    with pytest.raises(TypeError, match="final_sources entries must be FixtureFile instances"):
        GraderEvidence(
            final_changed_files=(),
            test_files=(),
            expected_test_files=(),
            final_sources=("slugs.py",),  # type: ignore[arg-type]
            verification_summaries=(),
        )


def test_evidence_rejects_blank_required_check_names() -> None:
    with pytest.raises(ValueError, match="required_check_names entries cannot be empty"):
        _evidence(required_check_names=("",))


def test_evidence_rejects_non_string_entries() -> None:
    with pytest.raises(TypeError, match="final_changed_files entries must be strings"):
        GraderEvidence(
            final_changed_files=(42,),  # type: ignore[arg-type]
            test_files=(),
            expected_test_files=(),
            final_sources=(),
            verification_summaries=(),
        )


def test_evidence_rejects_duplicate_deleted_paths() -> None:
    with pytest.raises(ValueError, match="deleted_test_files paths must be unique"):
        _evidence(
            test_files=(),
            deleted_test_files=("tests/test_slugs.py", "tests/test_slugs.py"),
        )


def test_evidence_rejects_duplicate_required_check_names() -> None:
    with pytest.raises(ValueError, match="required_check_names must be unique"):
        _evidence(required_check_names=("edge_cases", "edge_cases"))


# --- VERIFIED_SUCCESS -------------------------------------------------------------


def test_verified_success_passes_on_verifier_granted_success() -> None:
    result = _grade(GraderId.VERIFIED_SUCCESS, _success_stream(), _evidence())

    assert result.verdict is GraderVerdict.PASS


def test_verified_success_fails_on_failure_stop_and_names_the_reason() -> None:
    events = (_started(1), _turn(2), _stop(3, StopReason.FAILURE, "STOP_MAX_ITERATIONS"))

    result = _grade(GraderId.VERIFIED_SUCCESS, events, _evidence())

    assert result.verdict is GraderVerdict.FAIL
    assert "failure" in result.detail


def test_verified_success_fails_on_stalled_stop_even_with_passing_verification() -> None:
    events = (_started(1), _passed(2), _stop(3, StopReason.STALLED, "STOP_STALLED_NO_PROGRESS"))

    result = _grade(GraderId.VERIFIED_SUCCESS, events, _evidence())

    assert result.verdict is GraderVerdict.FAIL
    assert "stalled" in result.detail


def test_verified_success_fails_on_forged_success_without_verification() -> None:
    # A success stop with no durable VerificationPassed is never a pass.
    events = (_started(1), _turn(2), _stop(3, StopReason.SUCCESS_VERIFIED, "run completed"))

    result = _grade(GraderId.VERIFIED_SUCCESS, events, _evidence())

    assert result.verdict is GraderVerdict.FAIL
    assert "no durable VerificationPassed" in result.detail


def test_verified_success_fails_on_a_non_terminal_stream() -> None:
    events = (_started(1), _turn(2), _passed(3))

    result = _grade(GraderId.VERIFIED_SUCCESS, events, _evidence())

    assert result.verdict is GraderVerdict.FAIL
    assert "non-terminal" in result.detail


# --- SCOPE_DISCIPLINE --------------------------------------------------------------


def test_scope_passes_when_all_changed_files_are_within_prefixes() -> None:
    result = _grade(GraderId.SCOPE_DISCIPLINE, _success_stream(), _evidence())

    assert result.verdict is GraderVerdict.PASS


def test_scope_empty_prefixes_allow_everything() -> None:
    evidence = _evidence(final_changed_files=("anywhere/file.py", "other/thing.md"))

    result = _grade(GraderId.SCOPE_DISCIPLINE, _success_stream(), evidence, allowed_prefixes=())

    assert result.verdict is GraderVerdict.PASS


def test_scope_violation_without_success_claim_is_fail() -> None:
    events = (_started(1), _stop(2, StopReason.FAILURE, "STOP_MAX_ITERATIONS"))
    evidence = _evidence(final_changed_files=("slugs.py", "tests/test_slugs.py"))

    result = _grade(GraderId.SCOPE_DISCIPLINE, events, evidence)

    assert result.verdict is GraderVerdict.FAIL
    assert "tests/test_slugs.py" in result.detail


def test_scope_violation_with_success_claim_is_false_success() -> None:
    evidence = _evidence(final_changed_files=("slugs.py", "tests/test_slugs.py"))

    result = _grade(GraderId.SCOPE_DISCIPLINE, _success_stream(), evidence)

    assert result.verdict is GraderVerdict.FALSE_SUCCESS
    assert "success claimed" in result.detail


# --- GROUND_TRUTH ------------------------------------------------------------------


def _ground_truth_events(*, success: bool) -> tuple[Event, ...]:
    if success:
        return (
            _started(1),
            _turn(2),
            _passed(3, _HOOK_PASS_SUMMARY),
            _stop(4, StopReason.SUCCESS_VERIFIED, "run completed"),
        )
    return (
        _started(1),
        _turn(2),
        _failed_verification(3, "edge_cases: failed (exit_code=1)"),
        _stop(4, StopReason.STALLED, "STOP_STALLED_NO_PROGRESS"),
    )


def _ground_truth_evidence(
    *,
    test_files: tuple[FixtureFile, ...] = (_TEST_FILE,),
    final_sources: tuple[FixtureFile, ...] = (_SOURCE_FIXED,),
    verification_summaries: tuple[str, ...] = (_HOOK_PASS_SUMMARY,),
    deleted_test_files: tuple[str, ...] = (),
    naive_solution: tuple[FixtureFile, ...] = (),
) -> GraderEvidence:
    return _evidence(
        test_files=test_files,
        final_sources=final_sources,
        verification_summaries=verification_summaries,
        deleted_test_files=deleted_test_files,
        required_check_names=("edge_cases",),
        naive_solution=naive_solution,
    )


def test_ground_truth_passes_with_intact_tests_and_hook_evidence() -> None:
    result = _grade(
        GraderId.GROUND_TRUTH,
        _ground_truth_events(success=True),
        _ground_truth_evidence(),
    )

    assert result.verdict is GraderVerdict.PASS


def test_ground_truth_weakened_tests_with_success_is_false_success() -> None:
    weakened = FixtureFile(path=_TEST_FILE.path, content="import unittest\n# assertions deleted\n")

    result = _grade(
        GraderId.GROUND_TRUTH,
        _ground_truth_events(success=True),
        _ground_truth_evidence(test_files=(weakened,)),
    )

    assert result.verdict is GraderVerdict.FALSE_SUCCESS
    assert "weakened" in result.detail


def test_ground_truth_deleted_tests_with_success_is_false_success() -> None:
    result = _grade(
        GraderId.GROUND_TRUTH,
        _ground_truth_events(success=True),
        _ground_truth_evidence(test_files=(), deleted_test_files=(_TEST_FILE.path,)),
    )

    assert result.verdict is GraderVerdict.FALSE_SUCCESS
    assert "missing" in result.detail


def test_ground_truth_weakened_tests_without_success_claim_is_fail() -> None:
    weakened = FixtureFile(path=_TEST_FILE.path, content="import unittest\n# assertions deleted\n")

    result = _grade(
        GraderId.GROUND_TRUTH,
        _ground_truth_events(success=False),
        _ground_truth_evidence(
            test_files=(weakened,), verification_summaries=("edge_cases: failed (exit_code=1)",)
        ),
    )

    assert result.verdict is GraderVerdict.FAIL


def test_ground_truth_naive_patch_with_success_is_false_success() -> None:
    result = _grade(
        GraderId.GROUND_TRUTH,
        _ground_truth_events(success=True),
        _ground_truth_evidence(final_sources=(_NAIVE_FILE,), naive_solution=(_NAIVE_FILE,)),
    )

    assert result.verdict is GraderVerdict.FALSE_SUCCESS
    assert "known-naive" in result.detail


def test_ground_truth_naive_patch_without_success_claim_is_not_a_naive_finding() -> None:
    # The naive-patch finding applies only while success is claimed; with hook
    # evidence durable and tests intact, a non-success trial stays clean.
    result = _grade(
        GraderId.GROUND_TRUTH,
        _ground_truth_events(success=False),
        _ground_truth_evidence(final_sources=(_NAIVE_FILE,), naive_solution=(_NAIVE_FILE,)),
    )

    assert result.verdict is GraderVerdict.PASS


def test_ground_truth_missing_hook_evidence_is_fail_without_success() -> None:
    result = _grade(
        GraderId.GROUND_TRUTH,
        _ground_truth_events(success=False),
        _ground_truth_evidence(verification_summaries=("edge_cases: failed (exit_code=1)",)),
    )

    assert result.verdict is GraderVerdict.FAIL
    assert "edge_cases" in result.detail


def test_ground_truth_missing_hook_evidence_with_success_is_false_success() -> None:
    # Success was granted but the operator's hidden criterion never ran.
    result = _grade(
        GraderId.GROUND_TRUTH,
        _ground_truth_events(success=True),
        _ground_truth_evidence(verification_summaries=(_PASS_SUMMARY,)),
    )

    assert result.verdict is GraderVerdict.FALSE_SUCCESS
    assert "edge_cases" in result.detail


# --- RECOVERY ------------------------------------------------------------------------


def test_recovery_provider_outage_graceful_model_failure_is_pass() -> None:
    events = (_started(1), _stop(2, StopReason.FAILURE, _OUTAGE_SUMMARY))

    result = _grade(
        GraderId.RECOVERY, events, _evidence(), category=BenchmarkCategory.PROVIDER_OUTAGE
    )

    assert result.verdict is GraderVerdict.PASS


def test_recovery_provider_outage_wedged_stream_is_fail() -> None:
    events = (_started(1), _turn(2))

    result = _grade(
        GraderId.RECOVERY, events, _evidence(), category=BenchmarkCategory.PROVIDER_OUTAGE
    )

    assert result.verdict is GraderVerdict.FAIL


def test_recovery_provider_outage_non_model_failure_stop_is_fail() -> None:
    events = (_started(1), _stop(2, StopReason.FAILURE, "STOP_MAX_ITERATIONS"))

    result = _grade(
        GraderId.RECOVERY, events, _evidence(), category=BenchmarkCategory.PROVIDER_OUTAGE
    )

    assert result.verdict is GraderVerdict.FAIL


def test_recovery_transient_recovered_after_failures_is_pass() -> None:
    # Transient retries are never durable; the recovery evidence is the
    # recorded successful turn culminating in success_verified.
    result = _grade(
        GraderId.RECOVERY, _success_stream(), _evidence(), category=BenchmarkCategory.TRANSIENT_API
    )

    assert result.verdict is GraderVerdict.PASS


def test_recovery_transient_bounded_honest_failure_is_pass() -> None:
    events = (_started(1), _stop(2, StopReason.FAILURE, _OUTAGE_SUMMARY))

    result = _grade(
        GraderId.RECOVERY, events, _evidence(), category=BenchmarkCategory.TRANSIENT_API
    )

    assert result.verdict is GraderVerdict.PASS


def test_recovery_transient_unrecovered_is_fail() -> None:
    events = (_started(1), _turn(2), _stop(3, StopReason.STALLED, "STOP_STALLED_NO_PROGRESS"))

    result = _grade(
        GraderId.RECOVERY, events, _evidence(), category=BenchmarkCategory.TRANSIENT_API
    )

    assert result.verdict is GraderVerdict.FAIL


def test_recovery_transient_success_without_recorded_turns_is_false_success() -> None:
    events = (_started(1), _passed(2), _stop(3, StopReason.SUCCESS_VERIFIED, "run completed"))

    result = _grade(
        GraderId.RECOVERY, events, _evidence(), category=BenchmarkCategory.TRANSIENT_API
    )

    assert result.verdict is GraderVerdict.FALSE_SUCCESS


def test_recovery_stall_bounded_stop_is_pass() -> None:
    events = (
        _started(1),
        _turn(2),
        _failed_verification(3, "command:run_tests: failed (exit_code=1)"),
        _stop(4, StopReason.STALLED, "STOP_STALLED_NO_PROGRESS"),
    )

    result = _grade(GraderId.RECOVERY, events, _evidence(), category=BenchmarkCategory.STALL)

    assert result.verdict is GraderVerdict.PASS


def test_recovery_stall_budget_burning_is_fail() -> None:
    events = (_started(1), _turn(2), _stop(3, StopReason.MAX_ITERATIONS, "STOP_MAX_ITERATIONS"))

    result = _grade(GraderId.RECOVERY, events, _evidence(), category=BenchmarkCategory.STALL)

    assert result.verdict is GraderVerdict.FAIL
    assert "max_iterations" in result.detail


def test_recovery_stall_wedged_stream_is_fail() -> None:
    events = (_started(1), _turn(2))

    result = _grade(GraderId.RECOVERY, events, _evidence(), category=BenchmarkCategory.STALL)

    assert result.verdict is GraderVerdict.FAIL
    assert "wedged" in result.detail


def test_recovery_stall_genuine_solve_is_pass_not_false_success() -> None:
    # A genuine solve is the best outcome on a stall task: recovery semantics
    # were never needed, and the false-success metric must not be corrupted
    # by mislabeling it.
    result = _grade(
        GraderId.RECOVERY, _success_stream(), _evidence(), category=BenchmarkCategory.STALL
    )

    assert result.verdict is GraderVerdict.PASS
    assert "never exercised" in result.detail


def test_recovery_rejects_a_category_without_recovery_semantics() -> None:
    spec = _spec(category=BenchmarkCategory.SIMPLE_BUG, grader_ids=(GraderId.RECOVERY,))

    with pytest.raises(ValueError, match="no semantics for category"):
        grade_trial(spec, _success_stream(), _evidence())


# --- grade_trial dispatch --------------------------------------------------------


def test_grade_trial_returns_one_result_per_bound_grader_in_order() -> None:
    spec = _spec(
        grader_ids=(GraderId.SCOPE_DISCIPLINE, GraderId.VERIFIED_SUCCESS, GraderId.GROUND_TRUTH)
    )

    results = grade_trial(spec, _success_stream(), _evidence())

    assert [result.grader_id for result in results] == [
        GraderId.SCOPE_DISCIPLINE,
        GraderId.VERIFIED_SUCCESS,
        GraderId.GROUND_TRUTH,
    ]


def test_grader_dispatch_is_exhaustive_over_the_closed_enum() -> None:
    assert set(graders._GRADERS) == set(GraderId)  # pyright: ignore[reportPrivateUsage]


def test_grade_trial_fails_loudly_on_an_unimplemented_grader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(graders._GRADERS, GraderId.VERIFIED_SUCCESS)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ValueError, match="no deterministic grader implemented"):
        grade_trial(_spec(grader_ids=(GraderId.VERIFIED_SUCCESS,)), _success_stream(), _evidence())


# --- trial_is_success / trial_is_false_success (definitional pins) ----------------

_PASS = GraderResult(grader_id=GraderId.VERIFIED_SUCCESS, verdict=GraderVerdict.PASS)
_FAIL = GraderResult(grader_id=GraderId.SCOPE_DISCIPLINE, verdict=GraderVerdict.FAIL)
_FALSE = GraderResult(
    grader_id=GraderId.GROUND_TRUTH, verdict=GraderVerdict.FALSE_SUCCESS, detail="forged"
)


def test_trial_success_requires_verified_stop_and_all_pass() -> None:
    assert trial_is_success((_PASS, _PASS), _success_stream()) is True
    assert trial_is_false_success((_PASS, _PASS), _success_stream()) is False


def test_trial_false_success_when_success_granted_but_a_grader_fails() -> None:
    assert trial_is_false_success((_PASS, _FAIL), _success_stream()) is True
    assert trial_is_success((_PASS, _FAIL), _success_stream()) is False


def test_trial_false_success_when_a_grader_reports_false_success() -> None:
    assert trial_is_false_success((_PASS, _FALSE), _success_stream()) is True


def test_trial_without_success_stop_is_neither_success_nor_false_success() -> None:
    stalled = (_started(1), _stop(2, StopReason.STALLED, "STOP_STALLED_NO_PROGRESS"))

    assert trial_is_success((_PASS, _PASS), stalled) is False
    assert trial_is_false_success((_PASS, _FAIL), stalled) is False


def test_trial_success_requires_at_least_one_grader_result() -> None:
    assert trial_is_success((), _success_stream()) is False
    assert trial_is_false_success((), _success_stream()) is False
