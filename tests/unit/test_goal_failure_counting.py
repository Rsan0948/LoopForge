"""Supervisor failure allowance: lifetime by default, consecutive only when frozen opt-in."""

import pytest

from loopforge.domain.goals import (
    CallFailed,
    CallReserved,
    DecisionRecorded,
    GoalAction,
    GoalCreated,
    GoalPolicy,
    GoalState,
    SupervisorDecision,
    reduce_goal,
)
from loopforge.entrypoints.campaign_models import CampaignConfig, CodingCheck, CodingTaskConfig
from loopforge.entrypoints.supervisor import PreparedGoalConfig


def _policy(*, consecutive: bool) -> GoalPolicy:
    return GoalPolicy(
        objective="Repair the reconciliation app",
        contract_sha256="a" * 64,
        max_cost_microusd=1_000_000,
        deadline_epoch=2_000_000_000,
        max_decisions=20,
        max_failures=3,
        consecutive_failures=consecutive,
    )


def _fail(state: GoalState, key: str) -> GoalState:
    state = reduce_goal(state, CallReserved(key, 10))
    return reduce_goal(state, CallFailed(10, "Gemini returned an invalid decision envelope", 0))


def _decide(state: GoalState, key: str) -> GoalState:
    state = reduce_goal(state, CallReserved(key, 10))
    return reduce_goal(
        state, DecisionRecorded(SupervisorDecision(GoalAction.BLOCK, "stop", "plan"), 5, "")
    )


@pytest.mark.parametrize(("consecutive", "expected"), [(False, 2), (True, 0)])
def test_valid_decision_resets_failures_only_when_opted_in(
    consecutive: bool, expected: int
) -> None:
    state = reduce_goal(GoalState(), GoalCreated(_policy(consecutive=consecutive)))
    state = _fail(_fail(state, "k1"), "k2")
    assert state.failures == 2
    state = _decide(state, "k3")
    assert state.failures == expected


def test_consecutive_mode_still_exhausts_on_consecutive_failures() -> None:
    state = reduce_goal(GoalState(), GoalCreated(_policy(consecutive=True)))
    state = _fail(_fail(_fail(state, "k1"), "k2"), "k3")
    assert state.failures == 3
    with pytest.raises(ValueError, match="allowance exhausted"):
        reduce_goal(state, CallReserved("k4", 10))


def test_failure_mode_requires_boolean() -> None:
    with pytest.raises(ValueError, match="boolean"):
        GoalPolicy(
            objective="x",
            contract_sha256="a" * 64,
            max_cost_microusd=1,
            deadline_epoch=1,
            consecutive_failures="yes",  # type: ignore[arg-type]
        )


def test_default_failure_mode_is_omitted_from_frozen_intake_binding() -> None:
    config = PreparedGoalConfig(
        campaign=CampaignConfig(
            repository="/tmp/repo", project="brook", revision="a" * 40, scope=["src"]
        ),
        task=CodingTaskConfig(
            objective="Fix the incorrect addition",
            kind="bugfix",
            branch="goal/test",
            image="sha256:" + "a" * 64,
            allowed_paths=["src"],
            evidence="issue",
            checks=[CodingCheck(name="check_regression", argv=["/usr/local/bin/python"])],
        ),
        max_cost_microusd=100,
    )
    assert "consecutive_failures" not in config.model_dump_json()
    opted = config.model_copy(update={"consecutive_failures": True})
    assert '"consecutive_failures":true' in opted.model_dump_json()
