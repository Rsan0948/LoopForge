"""Partial credit inside a failed check feeds the progress signal; no network."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from loopforge.domain.events import VerificationFailed
from loopforge.domain.state import RunState, reduce_event
from loopforge.domain.types import EventId, RunId, RunStatus
from loopforge.domain.verification import CheckOutcome, compose_check_outcomes
from loopforge.workloads.repair import pytest_partial

NOW = datetime(2026, 10, 23, tzinfo=UTC)


def test_pytest_summary_gives_the_share_of_tests_passing() -> None:
    out = "..F.\n____ test_x ____\nE  assert 1\n==== 2 failed, 10 passed, 3 warnings in 1.24s ===="
    assert abs(cast("float", pytest_partial(out)) - 10 / 12) < 1e-9


@pytest.mark.parametrize(
    "stdout",
    [
        "E741 Ambiguous variable name\nFound 1 error.",
        "12 passed in 0.5s",
        "",
        "test printed: 99 passed",
    ],
    ids=["lint-only", "all-passed", "empty", "no-summary-line"],
)
def test_no_partial_credit_without_a_failing_pytest_summary(stdout: str) -> None:
    assert pytest_partial(stdout) is None


def test_only_the_final_summary_line_counts() -> None:
    out = "print: 500 passed in 1s\n...\n==== 3 failed, 1 passed in 2.0s ===="
    assert pytest_partial(out) == 0.25


def test_failed_check_partial_credit_raises_the_score_but_never_to_a_pass() -> None:
    passing = CheckOutcome(name="a", passed=True, detail="ok")
    failing = CheckOutcome(name="b", passed=False, detail="x", partial=0.9)
    composite = compose_check_outcomes((passing, failing))
    assert abs(composite.score - 0.95) < 1e-9
    assert composite.passed is False


@pytest.mark.parametrize(
    ("passed", "partial"),
    [(True, 0.5), (False, 1.0), (False, -0.1), (False, float("nan")), (False, True)],
)
def test_invalid_partial_credit_is_refused(passed: bool, partial: float) -> None:
    with pytest.raises(ValueError, match="partial"):
        CheckOutcome(name="b", passed=passed, detail="x", partial=partial)


def _failed(state: RunState, score: float, n: int) -> RunState:
    return reduce_event(
        state,
        VerificationFailed(
            event_id=EventId(f"evt_{n}"),
            run_id=state.run_id,
            occurred_at=NOW,
            sequence=n,
            summary=f"check failed {n}",
            score=score,
        ),
    )


def _reflecting() -> RunState:
    return replace(RunState(RunId("run_partial"), objective="Fix"), status=RunStatus.VERIFYING)


def test_progress_inside_a_check_resets_the_no_progress_counter() -> None:
    state = _failed(_reflecting(), 0.5, 1)
    state = _failed(replace(state, status=RunStatus.VERIFYING), 0.5, 2)
    assert state.consecutive_no_progress == 1
    # Same checks passing, but more tests pass inside the failing one.
    state = _failed(replace(state, status=RunStatus.VERIFYING), 0.5 + 0.4 / 2, 3)
    assert state.consecutive_no_progress == 0


def test_unchanged_or_worse_partial_credit_still_counts_as_no_progress() -> None:
    state = _failed(_reflecting(), 0.7, 1)
    for n, score in enumerate((0.7, 0.6, 0.5), start=2):
        state = _failed(replace(state, status=RunStatus.VERIFYING), score, n)
    assert state.consecutive_no_progress == 3


def _repeat(state: RunState, score: float, n: int) -> RunState:
    return reduce_event(
        replace(state, status=RunStatus.VERIFYING),
        VerificationFailed(
            event_id=EventId(f"evt_{n}"),
            run_id=state.run_id,
            occurred_at=NOW,
            sequence=n,
            summary=f"check failed {n}",
            score=score,
            repeat=True,
        ),
    )


def test_re_reported_verdicts_for_an_unchanged_candidate_count_no_attempt() -> None:
    state = _failed(_reflecting(), 0.5, 1)
    state = _failed(replace(state, status=RunStatus.VERIFYING), 0.5, 2)
    assert state.consecutive_no_progress == 1
    for n in range(3, 13):  # Ten checks of the same candidate: observations, not attempts.
        state = _repeat(state, 0.5, n)
    assert state.consecutive_no_progress == 1
    assert state.best_verification_score == 0.5


def test_a_repeat_never_resets_a_real_stall() -> None:
    state = _failed(_reflecting(), 0.7, 1)
    for n in range(2, 5):
        state = _failed(replace(state, status=RunStatus.VERIFYING), 0.6, n)
    state = _repeat(state, 0.9, 5)  # A repeat cannot claim progress either.
    assert state.consecutive_no_progress == 3
    assert state.best_verification_score == 0.7


def test_repeat_flag_round_trips_and_old_events_default_to_attempts() -> None:
    import json  # noqa: PLC0415

    from loopforge.adapters.json_events import JsonEventCodec  # noqa: PLC0415

    codec = JsonEventCodec()
    event = VerificationFailed(
        event_id=EventId("evt_r"),
        run_id=RunId("run_r"),
        occurred_at=NOW,
        sequence=1,
        summary="s",
        score=0.5,
        repeat=True,
    )
    encoded = codec.encode(event)
    assert codec.decode(encoded) == event
    legacy = json.loads(encoded)
    del legacy["event"]["repeat"]
    decoded = codec.decode(json.dumps(legacy))
    assert isinstance(decoded, VerificationFailed)
    assert decoded.repeat is False
