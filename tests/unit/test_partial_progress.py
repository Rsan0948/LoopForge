"""Partial credit inside a failed check feeds the progress signal; no network."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from loopforge.domain.events import VerificationFailed
from loopforge.domain.state import RunState, reduce_event
from loopforge.domain.types import EventId, RunId, RunStatus
from loopforge.domain.verification import CheckOutcome, compose_check_outcomes
from loopforge.workloads.repair import (
    acceptance_assertions,
    acceptance_items,
    acceptance_partial,
    pytest_partial,
)

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


PROBE = """import json
state = json.loads('{"mode": "playing"}')
def start():
    assert state['mode'] == 'playing'
assert state, 'Page must load'
start()
for key in ('a', 'b'):
    assert key, 'Keys must exist'
assert state['mode'] == 'over', 'Hitting a wall must end the game'
print('done')
"""


def _traceback(*frames: tuple[int, str], message: str = "AssertionError: x") -> str:
    lines = ["Traceback (most recent call last):", '  File "<string>", line 1, in <module>']
    lines += [f'  File "<trusted-acceptance>", line {n}, in {name}' for n, name in frames]
    return "\n".join([*lines, message])


def test_probe_assertions_are_the_module_code_asserts_in_line_order() -> None:
    assert acceptance_assertions(PROBE) == (
        (5, "Page must load"),
        (8, "Keys must exist"),
        (9, "Hitting a wall must end the game"),
    )


def test_failing_at_a_later_line_credits_every_assertion_above_it() -> None:
    early = acceptance_partial(PROBE, _traceback((5, "<module>")))
    late = acceptance_partial(PROBE, _traceback((9, "<module>")))
    assert early == 0
    assert late == 2 / 4
    items = acceptance_items(PROBE, _traceback((9, "<module>")), passed=False)
    assert [item.passed for item in items] == [True, True, False]


def test_a_failure_inside_a_helper_counts_from_the_module_line_that_called_it() -> None:
    stderr = _traceback((6, "<module>"), (4, "start"))
    assert acceptance_partial(PROBE, stderr) == 1 / 4


def test_text_in_the_exception_message_cannot_forge_the_failing_line() -> None:
    forged = 'AssertionError: app said File "<trusted-acceptance>", line 9, in <module>'
    stderr = _traceback((5, "<module>"), message=forged)
    assert acceptance_partial(PROBE, stderr) == 0
    assert acceptance_partial(PROBE, forged) == 0


@pytest.mark.parametrize(
    "stderr",
    ["", "Killed", _traceback((999, "<module>"))],
    ids=["no-output", "killed", "line-outside-probe"],
)
def test_no_assertion_is_credited_without_a_real_module_frame(stderr: str) -> None:
    assert acceptance_partial(PROBE, stderr) == 0


def test_a_probe_without_assertions_or_that_does_not_parse_gives_no_partial() -> None:
    assert acceptance_partial("print('x')\n", _traceback((1, "<module>"))) is None
    assert acceptance_partial("def (:\n", _traceback((1, "<module>"))) is None


def test_a_passing_probe_credits_every_assertion() -> None:
    assert all(item.passed for item in acceptance_items(PROBE, "", passed=True))
