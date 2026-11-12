"""Diagnosis allowances derive from committed attempts and measured improvement."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from loopforge.domain.events import (
    ModelAttemptFailed,
    ModelTurnRecorded,
    ToolSucceeded,
    VerificationFailed,
)
from loopforge.domain.policy import ControlPolicy
from loopforge.domain.state import RunState, reduce_event
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import (
    ActionId,
    BudgetLimit,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    RunStatus,
)
from loopforge.domain.verification import CheckOutcome, VerificationProgress

NOW = datetime(2026, 10, 26, tzinfo=UTC)


@pytest.mark.parametrize("failed", [False, True])
def test_each_committed_model_attempt_consumes_the_diagnosis_allowance(failed: bool) -> None:
    event = (
        ModelAttemptFailed(
            event_id=EventId("e"),
            run_id=RunId("r"),
            sequence=1,
            occurred_at=NOW,
            reason_code="MODEL_TIMEOUT",
            summary="timeout",
        )
        if failed
        else ModelTurnRecorded(
            event_id=EventId("e"),
            run_id=RunId("r"),
            sequence=1,
            occurred_at=NOW,
            action_id=ActionId("a"),
            provider="fake",
            model="m",
        )
    )
    state = reduce_event(RunState(RunId("r"), status=RunStatus.READY), event)
    assert state.model_attempts_since_progress == 1


@pytest.mark.parametrize("passed", [("a",), ("b",), ()])
def test_equal_regressed_or_incomparable_checks_do_not_reset_attempts(
    passed: tuple[str, ...],
) -> None:
    best = VerificationProgress(basis="frozen", checks=("a", "b"), passed=("a",))
    state = RunState(
        RunId("r"),
        status=RunStatus.VERIFYING,
        best_verification_progress=best,
        model_attempts_since_progress=12,
    )
    progress = replace(best, passed=passed)
    event = VerificationFailed(
        event_id=EventId("v"),
        run_id=state.run_id,
        sequence=1,
        occurred_at=NOW,
        summary="new text",
        score=0.99,
        progress=progress,
        outcomes=tuple(CheckOutcome(name=name, passed=True, detail="measured") for name in passed),
    )
    assert reduce_event(state, event).model_attempts_since_progress == 12


def test_strict_measured_improvement_resets_attempts_and_repeats_do_not() -> None:
    best = VerificationProgress(basis="frozen", checks=("a", "b", "c"), passed=("a",))
    state = RunState(
        RunId("r"),
        status=RunStatus.VERIFYING,
        best_verification_progress=best,
        model_attempts_since_progress=12,
    )
    progress = replace(best, passed=("a", "b"))
    event = VerificationFailed(
        event_id=EventId("v"),
        run_id=state.run_id,
        sequence=1,
        occurred_at=NOW,
        summary="measured",
        score=0.7,
        progress=progress,
        outcomes=tuple(
            CheckOutcome(name=name, passed=True, detail="measured") for name in progress.passed
        ),
    )
    improved = reduce_event(state, event)
    assert improved.model_attempts_since_progress == 0
    repeated = reduce_event(
        replace(improved, status=RunStatus.VERIFYING, model_attempts_since_progress=8),
        replace(event, sequence=2, repeat=True),
    )
    assert repeated.model_attempts_since_progress == 8


def test_inconclusive_result_cannot_reset_attempts() -> None:
    state = RunState(RunId("r"), status=RunStatus.VERIFYING, model_attempts_since_progress=12)
    event = VerificationFailed(
        event_id=EventId("v"),
        run_id=state.run_id,
        sequence=1,
        occurred_at=NOW,
        summary="infra",
        inconclusive=True,
    )
    assert reduce_event(state, event).model_attempts_since_progress == 12


@pytest.mark.parametrize(
    ("write_at", "elapsed", "stopped"),
    [
        (None, 479, False),
        (None, 480, True),
        (120, 2000, False),
        (481, 482, True),
    ],
)
def test_first_write_deadline_excludes_subsequent_coding_and_review_time(
    write_at: int | None,
    elapsed: int,
    stopped: bool,
) -> None:
    state = RunState(
        RunId("r"),
        started_at=NOW,
        first_local_write_at=NOW + timedelta(seconds=write_at) if write_at is not None else None,
    )
    policy = ControlPolicy(
        BudgetLimit(max_cost_usd=1, max_iterations=100), first_local_write_seconds=480
    )
    decision = policy.evaluate(state, now=NOW + timedelta(seconds=elapsed))
    assert (decision.reason_code == "STOP_FIRST_WRITE_DEADLINE") is stopped
    if write_at == 481:
        assert (
            policy.evaluate(
                replace(state, last_verification_passed=True), now=NOW + timedelta(seconds=elapsed)
            ).reason_code
            == "STOP_FIRST_WRITE_DEADLINE"
        )


@pytest.mark.parametrize("write", [False, True])
def test_only_successful_code_owned_write_metadata_sets_the_first_write(write: bool) -> None:
    metadata = ToolMetadata(
        name="edit" if write else "read",
        risk=RiskLevel.LOCAL_WRITE if write else RiskLevel.READ_ONLY,
        required_permission=Permission.LOCAL_WRITE if write else Permission.READ,
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NONE if write else IdempotencyClass.NOT_APPLICABLE,
        approval=ApprovalClass.NONE,
        timeout_seconds=5,
        side_effect=SideEffectClass.LOCAL_WRITE if write else SideEffectClass.READ_ONLY,
    )
    state = RunState(
        RunId("r"), status=RunStatus.ACTING, current_action_id="a", current_tool_metadata=metadata
    )
    event = ToolSucceeded(
        event_id=EventId("s"),
        run_id=state.run_id,
        occurred_at=NOW,
        sequence=1,
        action_id=ActionId("a"),
        attempt=1,
        observation="PASS! edited source",
    )
    result = reduce_event(state, event)
    assert result.first_local_write_at == (NOW if write else None)
