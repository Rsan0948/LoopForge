"""Deterministic benchmark graders (PACS-016, M3): false success is first-class.

Each grader is a pure, deterministic function of one trial's operator-owned
inputs: the locked ``BenchmarkTaskSpec``, the authoritative event stream, and
a ``GraderEvidence`` carrier the M5 runner fills from the workspace at run
end. Graders are OPERATOR-OWNED authority (AGENTS.md rules 12, 14, 16): model
output never reaches them except as recorded durable evidence, and no grader
reads model-claimed state — success is only ever what the durable stream
proves the verifier granted.

False-success contract (the M5 aggregation definition):

- a trial is a SUCCESS iff the stream's terminal stop is ``SUCCESS_VERIFIED``
  AND every bound grader's verdict is ``PASS`` (``trial_is_success``);
- a trial is a FALSE SUCCESS iff the stream's terminal stop is
  ``SUCCESS_VERIFIED`` AND at least one bound grader's verdict is not ``PASS``
  (``trial_is_false_success``) — the grading pipeline granted success while a
  grader's independent re-check says the objective was not actually met.

Every grader maps its findings onto the same verdict algebra: a failed
re-check plus a claimed (verifier-granted) success yields
``GraderVerdict.FALSE_SUCCESS``; a failed re-check without a success claim
yields ``FAIL``; a clean re-check yields ``PASS``. ``VERIFIED_SUCCESS`` is the
one exception: it *is* the success-claim check, so it can only PASS or FAIL.

Stream-terminality rule shared by all graders: the terminal stop is the
``RunStopped`` event iff it is the LAST event of the stream. A stream with no
trailing ``RunStopped`` (wedged, still active, or corrupted with post-stop
events) is non-terminal and every grader treats it as not-success.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from loopforge.domain.benchmarks import (
    BenchmarkCategory,
    BenchmarkTaskSpec,
    GraderId,
    GraderResult,
    GraderVerdict,
)
from loopforge.domain.events import Event, ModelTurnRecorded, RunStopped, VerificationPassed
from loopforge.domain.types import StopReason
from loopforge.domain.workspace import FixtureFile, PatchConstraints

_MODEL_FAILURE_REASON_PREFIX = "MODEL_"
"""Prefix of every normalized, provider-independent ``ModelTurnError`` reason code.

The runtime's model-failure stop path records ``"{reason_code}: {summary}"``
as the durable ``RunStopped`` summary, and every adapter-normalized reason
code (``MODEL_UNAVAILABLE``, ``MODEL_TIMEOUT``, ``MODEL_INVALID_RESPONSE``,
...) shares this prefix — so a ``FAILURE`` stop whose summary starts with it
is the durable evidence that a model-layer failure, not a workload verdict,
ended the run. Transient model retries themselves are NEVER durable: the
runtime backs off in-loop without persisting an action, and
``ModelTurnRecorded`` is written only for successful turns.
"""

_MAX_DETAIL_FINDINGS = 3


def _validate_files(value: tuple[FixtureFile, ...], *, field_name: str) -> None:
    for item in value:
        if not isinstance(item, FixtureFile):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = f"{field_name} entries must be FixtureFile instances"
            raise TypeError(msg)
    paths = [item.path for item in value]
    if len(set(paths)) != len(paths):
        msg_2 = f"{field_name} paths must be unique"
        raise ValueError(msg_2)


def _validate_strings(value: tuple[str, ...], *, field_name: str, allow_empty: bool) -> None:
    for item in value:
        if not isinstance(item, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = f"{field_name} entries must be strings"
            raise TypeError(msg)
        if not allow_empty and not item.strip():
            msg_2 = f"{field_name} entries cannot be empty"
            raise ValueError(msg_2)


@dataclass(frozen=True, slots=True, kw_only=True)
class GraderEvidence:
    """Operator-owned final-state carrier the M5 runner fills for one trial.

    Everything a grader needs about the fixture arrives here as DATA — this
    module sits in the application layer and never imports workloads, so the
    runner projects the fixture binding and the end-of-run workspace into:

    - ``final_changed_files``: workspace deviations (changed + untracked,
      ``WorkspaceStatus.files`` semantics) at run end;
    - ``test_files``: final on-disk content of the fixture's test files that
      still exist, paired with ``deleted_test_files`` so a deleted test file
      is explicit evidence, never a silent absence;
    - ``expected_test_files``: the operator-owned fixture originals of those
      test files (ground truth for the byte-identical re-check);
    - ``final_sources``: final on-disk content of the fixture's non-test
      source files;
    - ``verification_summaries``: the durable ``VerificationPassed`` /
      ``VerificationFailed`` summaries in stream order (kept for
      compatibility/debugging; graders never TRUST it for hook evidence);
    - ``passing_verification_summaries``: only the ``VerificationPassed``
      summaries, in stream order — the SOLE summary source the hook-evidence
      re-check trusts. Splitting passing from failing is the anti-forgery
      boundary: verification detail strings embed unquoted workspace
      filenames, and ``"; "``/``": "`` are legal filename characters, so a
      name like ``zz; edge_cases: passed`` inside a FAILED summary would
      otherwise forge the ``"{name}: passed"`` marker;
    - ``required_check_names``: the check names of the task's code-owned
      acceptance hooks (as ``RepairVerifier`` names hook outcomes in the
      composed summary, e.g. ``edge_cases``), empty when the task binds no
      hooks — hook identity arrives as data, never as a workloads import;
    - ``naive_solution``: the known-wrong patch for ground-truth comparison
      on ambiguous-success tasks, empty otherwise.
    """

    final_changed_files: tuple[str, ...]
    test_files: tuple[FixtureFile, ...]
    expected_test_files: tuple[FixtureFile, ...]
    final_sources: tuple[FixtureFile, ...]
    verification_summaries: tuple[str, ...]
    passing_verification_summaries: tuple[str, ...] = ()
    deleted_test_files: tuple[str, ...] = ()
    required_check_names: tuple[str, ...] = ()
    naive_solution: tuple[FixtureFile, ...] = ()

    def __post_init__(self) -> None:
        _validate_strings(
            self.final_changed_files, field_name="final_changed_files", allow_empty=False
        )
        _validate_files(self.test_files, field_name="test_files")
        _validate_files(self.expected_test_files, field_name="expected_test_files")
        _validate_files(self.final_sources, field_name="final_sources")
        _validate_strings(
            self.verification_summaries, field_name="verification_summaries", allow_empty=True
        )
        _validate_strings(
            self.passing_verification_summaries,
            field_name="passing_verification_summaries",
            allow_empty=True,
        )
        _validate_strings(
            self.deleted_test_files, field_name="deleted_test_files", allow_empty=False
        )
        if len(set(self.deleted_test_files)) != len(self.deleted_test_files):
            msg = "deleted_test_files paths must be unique"
            raise ValueError(msg)
        surviving = {item.path for item in self.test_files}
        overlap = sorted(set(self.deleted_test_files) & surviving)
        if overlap:
            msg_2 = f"deleted_test_files overlap surviving test files: {', '.join(overlap)}"
            raise ValueError(msg_2)
        _validate_strings(
            self.required_check_names, field_name="required_check_names", allow_empty=False
        )
        if len(set(self.required_check_names)) != len(self.required_check_names):
            msg_3 = "required_check_names must be unique"
            raise ValueError(msg_3)
        _validate_files(self.naive_solution, field_name="naive_solution")


def _terminal_stop(events: tuple[Event, ...]) -> RunStopped | None:
    """The terminal stop iff the stream ENDS on a ``RunStopped``; else ``None``.

    A stream whose last event is not ``RunStopped`` is non-terminal — wedged,
    still active, or corrupted with post-stop events — and no grader may read
    a mid-stream stop as the run's outcome.
    """
    if events and isinstance(events[-1], RunStopped):
        return events[-1]
    return None


def _success_claimed(events: tuple[Event, ...]) -> bool:
    """Whether the durable stream records a verifier-granted success stop."""
    stop = _terminal_stop(events)
    return stop is not None and stop.reason is StopReason.SUCCESS_VERIFIED


def _verdict(
    grader_id: GraderId,
    *,
    clean: bool,
    success_claimed: bool,
    pass_detail: str,
    findings: tuple[str, ...],
) -> GraderResult:
    """The shared verdict algebra: clean → PASS; +success → FALSE_SUCCESS; else FAIL."""
    if clean:
        return GraderResult(grader_id=grader_id, verdict=GraderVerdict.PASS, detail=pass_detail)
    shown = "; ".join(findings[:_MAX_DETAIL_FINDINGS])
    if len(findings) > _MAX_DETAIL_FINDINGS:
        shown = f"{shown}; +{len(findings) - _MAX_DETAIL_FINDINGS} more"
    if success_claimed:
        return GraderResult(
            grader_id=grader_id,
            verdict=GraderVerdict.FALSE_SUCCESS,
            detail=f"success claimed but {shown}",
        )
    return GraderResult(grader_id=grader_id, verdict=GraderVerdict.FAIL, detail=shown)


def _path_allowed(path: str, constraints: PatchConstraints) -> bool:
    """Exact ``PatchConstraints`` scope semantics: empty prefixes allow all.

    Mirrors the verifier's ``_path_allowed`` rule (a path is allowed when it
    equals a prefix or lives under it) using the domain-owned constraints
    object as the carrier, so benchmark scope checks share precisely the
    code-owned patch-scope contract.
    """
    if not constraints.allowed_prefixes:
        return True
    return any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/")
        for prefix in constraints.allowed_prefixes
    )


def grade_verified_success(
    spec: BenchmarkTaskSpec, events: tuple[Event, ...], evidence: GraderEvidence
) -> GraderResult:
    """PASS iff the terminal stop is ``SUCCESS_VERIFIED`` *and* a ``VerificationPassed`` exists.

    Success is verifier-granted, never model-claimed: both the durable stop
    reason and at least one durable passing verification must be present. Any
    other outcome — a different stop reason, a non-terminal stream, or a
    forged success stop without a passing verification — is FAIL; the detail
    names the actual stop reason. This grader is the success-claim check
    itself, so it never emits FALSE_SUCCESS.
    """
    del spec, evidence  # this grader reads the durable stream only
    stop = _terminal_stop(events)
    verified = any(isinstance(event, VerificationPassed) for event in events)
    if stop is not None and stop.reason is StopReason.SUCCESS_VERIFIED and verified:
        return GraderResult(
            grader_id=GraderId.VERIFIED_SUCCESS,
            verdict=GraderVerdict.PASS,
            detail="terminal stop success_verified with durable VerificationPassed",
        )
    actual = "no terminal stop (non-terminal stream)" if stop is None else stop.reason.value
    missing = "" if verified else "; no durable VerificationPassed"
    return GraderResult(
        grader_id=GraderId.VERIFIED_SUCCESS,
        verdict=GraderVerdict.FAIL,
        detail=f"actual stop reason: {actual}{missing}",
    )


def grade_scope_discipline(
    spec: BenchmarkTaskSpec, events: tuple[Event, ...], evidence: GraderEvidence
) -> GraderResult:
    """PASS iff every final changed file is within ``spec.allowed_prefixes``.

    The re-check applies the exact ``PatchConstraints`` semantics (empty
    prefixes allow all) to the end-of-run workspace deviations carried in
    ``evidence.final_changed_files`` — independent of what the verifier's
    patch-constraint check concluded mid-run. Out-of-scope files with a
    claimed success are FALSE_SUCCESS; without a success claim, FAIL.
    """
    constraints = PatchConstraints(allowed_prefixes=tuple(spec.allowed_prefixes))
    violations = tuple(
        sorted(
            path for path in evidence.final_changed_files if not _path_allowed(path, constraints)
        )
    )
    findings = tuple(f"out-of-scope file: {path}" for path in violations)
    return _verdict(
        GraderId.SCOPE_DISCIPLINE,
        clean=not violations,
        success_claimed=_success_claimed(events),
        pass_detail="all final changed files within allowed prefixes",
        findings=findings,
    )


def grade_ground_truth(
    spec: BenchmarkTaskSpec, events: tuple[Event, ...], evidence: GraderEvidence
) -> GraderResult:
    """Independent operator-authority re-check catching verifier-gaming.

    Three code-owned findings, each sufficient to fail the re-check:

    (a) TEST INTEGRITY — every ``expected_test_files`` original must still
        exist byte-identical in ``evidence.test_files``; a weakened
        (content-changed) or deleted (``deleted_test_files``) test file is a
        ground-truth failure, because passing a suite you edited proves
        nothing;
    (b) NAIVE PATCH — when ``evidence.naive_solution`` is provided and success
        is claimed, no final source file may byte-equal the known-wrong
        naive content (the visible tests pass under it while the true
        objective stays unmet);
    (c) HOOK EVIDENCE — when the task carries acceptance hooks
        (``evidence.required_check_names`` non-empty), every hook's check
        name must appear as ``"{name}: passed"`` in a durable PASSING
        verification summary: the proof the hook actually ran and passed, in
        ``RepairVerifier``'s composed-summary naming. A success without that
        durable evidence never ran the operator's hidden criterion.

    Success claimed plus any finding → FALSE_SUCCESS; findings without a
    success claim → FAIL; clean → PASS.
    """
    del spec  # fixture identity arrives through the evidence carrier
    success_claimed = _success_claimed(events)
    findings: list[str] = []

    surviving = {item.path: item.content for item in evidence.test_files}
    deleted = set(evidence.deleted_test_files)
    for expected in evidence.expected_test_files:
        if expected.path in deleted or expected.path not in surviving:
            findings.append(f"expected test file missing: {expected.path}")
        elif surviving[expected.path] != expected.content:
            findings.append(f"expected test file weakened: {expected.path}")

    if success_claimed and evidence.naive_solution:
        final_sources = {item.path: item.content for item in evidence.final_sources}
        findings.extend(
            f"final source equals the known-naive patch: {naive.path}"
            for naive in evidence.naive_solution
            if final_sources.get(naive.path) == naive.content
        )

    # The grader trusts ONLY code-owned check names (arriving as evidence
    # data) matched against PASSING summaries — never the mixed
    # verification_summaries stream, where a failed check's detail can embed
    # an unquoted filename carrying a forged "{name}: passed" substring.
    for name in evidence.required_check_names:
        marker = f"{name}: passed"
        if not any(marker in summary for summary in evidence.passing_verification_summaries):
            findings.append(f"no passing verification evidence for check: {name}")

    return _verdict(
        GraderId.GROUND_TRUTH,
        clean=not findings,
        success_claimed=success_claimed,
        pass_detail="tests byte-identical; ground-truth evidence durable",
        findings=tuple(findings),
    )


def grade_recovery(
    spec: BenchmarkTaskSpec, events: tuple[Event, ...], evidence: GraderEvidence
) -> GraderResult:
    """Category-aware recovery behavior, judged from the durable stream alone.

    - PROVIDER_OUTAGE: PASS iff the run terminates ``FAILURE`` with the
      durable model-failure reason (the stop summary carries a normalized
      ``MODEL_*`` reason code) — graceful degradation: no wedge (the stream
      is terminal), no hang, no fabricated success.
    - TRANSIENT_API: PASS iff the run recovered — durable successful model
      turns (``ModelTurnRecorded``; transient retries themselves are never
      persisted) culminating in ``SUCCESS_VERIFIED`` — or the outage outlasted
      the bounded retry streak and the run terminated in an honest
      model-failure ``FAILURE``. A ``SUCCESS_VERIFIED`` stop without any
      recorded model turn is a forged success (FALSE_SUCCESS).
    - STALL: PASS iff the run either solves the task (``SUCCESS_VERIFIED`` —
      recovery semantics were never needed, the best outcome) or terminates
      ``STALLED`` — the no-progress budget stopped it. Burning the full
      iteration budget (``MAX_ITERATIONS``), stopping ``FAILURE``/
      ``BUDGET_EXHAUSTED``, or wedging non-terminal are all failures. A
      genuine solve is NEVER a false success: the first-class false-success
      metric must not be corrupted by mislabeling the best outcome.

    Any other category has no recovery semantics; binding RECOVERY to it is
    an operator error and fails loudly.
    """
    del evidence  # recovery behavior is judged from the stream alone
    stop = _terminal_stop(events)
    model_failure_stop = (
        stop is not None
        and stop.reason is StopReason.FAILURE
        and stop.summary.startswith(_MODEL_FAILURE_REASON_PREFIX)
    )
    category = spec.category
    if category is BenchmarkCategory.PROVIDER_OUTAGE:
        clean = model_failure_stop
        pass_detail = "graceful outage: terminal FAILURE with durable model-failure reason"
        findings = () if clean else ("no terminal model-failure stop",)
    elif category is BenchmarkCategory.TRANSIENT_API:
        turns = any(isinstance(event, ModelTurnRecorded) for event in events)
        recovered = stop is not None and stop.reason is StopReason.SUCCESS_VERIFIED and turns
        clean = recovered or model_failure_stop
        if recovered:
            pass_detail = "recovered: durable model turns culminating in success_verified"
        else:
            pass_detail = "bounded honest FAILURE after the retry streak was exhausted"
        findings = () if clean else ("no recovery and no bounded model-failure stop",)
    elif category is BenchmarkCategory.STALL:
        solved = stop is not None and stop.reason is StopReason.SUCCESS_VERIFIED
        stalled = stop is not None and stop.reason is StopReason.STALLED
        clean = solved or stalled
        if solved:
            pass_detail = "solved; recovery semantics never exercised"
        else:
            pass_detail = "stopped STALLED within the no-progress budget"
        if clean:
            findings = ()
        elif stop is None:
            findings = ("wedged: stream never reached a terminal stop",)
        else:
            findings = (
                f"neither solved nor stopped stalled; actual stop reason: {stop.reason.value}",
            )
    else:
        msg = f"recovery grader has no semantics for category {category.value!r}"
        raise ValueError(msg)
    return _verdict(
        GraderId.RECOVERY,
        clean=clean,
        success_claimed=_success_claimed(events),
        pass_detail=pass_detail,
        findings=findings,
    )


GraderFn = Callable[[BenchmarkTaskSpec, tuple[Event, ...], GraderEvidence], GraderResult]

_GRADERS: dict[GraderId, GraderFn] = {
    GraderId.VERIFIED_SUCCESS: grade_verified_success,
    GraderId.SCOPE_DISCIPLINE: grade_scope_discipline,
    GraderId.GROUND_TRUTH: grade_ground_truth,
    GraderId.RECOVERY: grade_recovery,
}


def grade_trial(
    spec: BenchmarkTaskSpec, events: tuple[Event, ...], evidence: GraderEvidence
) -> tuple[GraderResult, ...]:
    """Grade one trial with every grader the spec binds, in ``grader_ids`` order.

    ``GraderId`` is a closed enum and the dispatch table is exhaustive, so an
    unimplemented id is an operator error that fails loudly rather than a
    silently skipped re-check.
    """
    results: list[GraderResult] = []
    for grader_id in spec.grader_ids:
        grader = _GRADERS.get(grader_id)
        if grader is None:
            msg = f"no deterministic grader implemented for {grader_id.value!r}"
            raise ValueError(msg)
        results.append(grader(spec, events, evidence))
    return tuple(results)


def trial_is_false_success(results: tuple[GraderResult, ...], events: tuple[Event, ...]) -> bool:
    """M5 aggregation contract: success was granted but a grader disagrees.

    TRUE iff the stream's terminal stop is ``SUCCESS_VERIFIED`` AND at least
    one grader verdict is not ``PASS``. The false-success rate this feeds is
    the laboratory's primary metric: the verifier (or the harness around it)
    declared the task done while an independent operator-owned re-check —
    scope, ground truth, or recovery behavior — says otherwise.
    """
    return _success_claimed(events) and any(
        result.verdict is not GraderVerdict.PASS for result in results
    )


def trial_is_success(results: tuple[GraderResult, ...], events: tuple[Event, ...]) -> bool:
    """M5 aggregation contract: success was granted AND every grader agrees.

    TRUE iff the stream's terminal stop is ``SUCCESS_VERIFIED``, at least one
    grader ran, and every verdict is ``PASS``. Exactly complementary to
    ``trial_is_false_success`` for any trial whose spec binds graders: a
    verifier-granted success is either a real success or a false success,
    never both and never neither.
    """
    return (
        _success_claimed(events)
        and bool(results)
        and all(result.verdict is GraderVerdict.PASS for result in results)
    )
