from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from loopforge.domain.reliability import (
    ReliabilityPolicy,
    RetryDecision,
    RetrySettings,
    ToolFailureClass,
    idempotency_key_for,
)
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import ActionId, Permission, RiskLevel, RunId

ACTION = ActionId("act-1")
RUN = RunId("run-1")


def _metadata(
    *,
    retry: RetryClass = RetryClass.SAFE,
    idempotency: IdempotencyClass = IdempotencyClass.NATURAL,
    side_effect: SideEffectClass = SideEffectClass.READ_ONLY,
) -> ToolMetadata:
    return ToolMetadata(
        name="tool",
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=side_effect,
        retry=retry,
        idempotency=idempotency,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


# --- Failure classification --------------------------------------------------


def test_tool_failure_class_values_round_trip_through_strings() -> None:
    assert ToolFailureClass("transient") is ToolFailureClass.TRANSIENT
    assert ToolFailureClass("permanent") is ToolFailureClass.PERMANENT
    assert ToolFailureClass("ambiguous_outcome") is ToolFailureClass.AMBIGUOUS_OUTCOME


def test_tool_failure_class_partition_is_exact() -> None:
    assert set(ToolFailureClass) == {
        ToolFailureClass.TRANSIENT,
        ToolFailureClass.PERMANENT,
        ToolFailureClass.AMBIGUOUS_OUTCOME,
    }


# --- RetrySettings validation -------------------------------------------------


def test_retry_settings_defaults_are_bounded_and_valid() -> None:
    settings = RetrySettings()
    assert settings.max_attempts == 3
    assert settings.base_delay_seconds == 0.25
    assert settings.max_delay_seconds == 4.0
    assert settings.jitter_fraction == 0.20


@pytest.mark.parametrize("max_attempts", [0, -1, -100])
def test_retry_settings_rejects_non_positive_max_attempts(max_attempts: int) -> None:
    with pytest.raises(ValueError, match="max_attempts must be positive"):
        RetrySettings(max_attempts=max_attempts)


@pytest.mark.parametrize(
    ("base_delay_seconds", "max_delay_seconds"),
    [(-0.1, 1.0), (-100.0, 1.0), (0.5, -0.1)],
)
def test_retry_settings_rejects_negative_delays(
    base_delay_seconds: float, max_delay_seconds: float
) -> None:
    with pytest.raises(ValueError, match="retry delays cannot be negative"):
        RetrySettings(base_delay_seconds=base_delay_seconds, max_delay_seconds=max_delay_seconds)


def test_retry_settings_rejects_max_delay_below_base_delay() -> None:
    with pytest.raises(ValueError, match="max_delay_seconds cannot be below base_delay_seconds"):
        RetrySettings(base_delay_seconds=2.0, max_delay_seconds=1.0)


@pytest.mark.parametrize("jitter_fraction", [-0.01, -1.0, 1.01, 2.0])
def test_retry_settings_rejects_jitter_outside_unit_interval(jitter_fraction: float) -> None:
    with pytest.raises(ValueError, match="jitter_fraction must be between 0 and 1"):
        RetrySettings(jitter_fraction=jitter_fraction)


@pytest.mark.parametrize("jitter_fraction", [0.0, 1.0])
def test_retry_settings_accepts_jitter_boundaries(jitter_fraction: float) -> None:
    settings = RetrySettings(jitter_fraction=jitter_fraction)
    assert settings.jitter_fraction == jitter_fraction


def test_retry_settings_accepts_zero_delays() -> None:
    settings = RetrySettings(base_delay_seconds=0.0, max_delay_seconds=0.0)
    assert settings.base_delay_seconds == 0.0
    assert settings.max_delay_seconds == 0.0


# --- ReliabilityPolicy validation ----------------------------------------------


@pytest.mark.parametrize("threshold", [0, -1, -50])
def test_reliability_policy_rejects_non_positive_circuit_threshold(threshold: int) -> None:
    with pytest.raises(ValueError, match="circuit_failure_threshold must be positive"):
        ReliabilityPolicy(circuit_failure_threshold=threshold)


# --- retry_decision: attempt budget --------------------------------------------


@pytest.mark.parametrize("attempt", [3, 4, 100])
def test_retry_denied_once_attempts_are_exhausted(attempt: int) -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(),
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=attempt,
        action_id=ACTION,
    )
    assert decision.should_retry is False
    assert decision.reason_code == "RETRY_ATTEMPTS_EXHAUSTED"
    assert decision.next_attempt is None
    assert decision.delay_seconds == 0.0


def test_exhausted_attempts_take_precedence_over_other_denial_rules() -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(retry=RetryClass.NEVER),
        failure_class=ToolFailureClass.PERMANENT,
        attempt=3,
        action_id=ACTION,
    )
    assert decision.reason_code == "RETRY_ATTEMPTS_EXHAUSTED"


# --- retry_decision: tool retry class -------------------------------------------


@pytest.mark.parametrize(
    "failure_class",
    [ToolFailureClass.TRANSIENT, ToolFailureClass.AMBIGUOUS_OUTCOME],
)
def test_retry_never_tools_are_never_retried(failure_class: ToolFailureClass) -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(retry=RetryClass.NEVER),
        failure_class=failure_class,
        attempt=1,
        action_id=ACTION,
    )
    assert decision.should_retry is False
    assert decision.reason_code == "RETRY_TOOL_POLICY_NEVER"
    assert decision.next_attempt is None
    assert decision.delay_seconds == 0.0


def test_retry_never_takes_precedence_over_permanent_failure() -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(retry=RetryClass.NEVER),
        failure_class=ToolFailureClass.PERMANENT,
        attempt=1,
        action_id=ACTION,
    )
    assert decision.reason_code == "RETRY_TOOL_POLICY_NEVER"


# --- retry_decision: failure class rules -----------------------------------------


@pytest.mark.parametrize("retry", [RetryClass.SAFE, RetryClass.TRANSIENT_ONLY])
def test_permanent_failures_are_never_retried(retry: RetryClass) -> None:
    metadata = (
        _metadata(retry=retry)
        if retry is RetryClass.SAFE
        else _metadata(retry=retry, idempotency=IdempotencyClass.KEYED)
    )
    decision = ReliabilityPolicy().retry_decision(
        metadata=metadata,
        failure_class=ToolFailureClass.PERMANENT,
        attempt=1,
        action_id=ACTION,
    )
    assert decision.should_retry is False
    assert decision.reason_code == "RETRY_FAILURE_PERMANENT"
    assert decision.next_attempt is None
    assert decision.delay_seconds == 0.0


def test_ambiguous_failure_without_idempotency_guarantee_is_not_retried() -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(idempotency=IdempotencyClass.NOT_APPLICABLE),
        failure_class=ToolFailureClass.AMBIGUOUS_OUTCOME,
        attempt=1,
        action_id=ACTION,
    )
    assert decision.should_retry is False
    assert decision.reason_code == "RETRY_AMBIGUOUS_NOT_IDEMPOTENT"
    assert decision.next_attempt is None
    assert decision.delay_seconds == 0.0


@pytest.mark.parametrize("idempotency", [IdempotencyClass.NATURAL, IdempotencyClass.KEYED])
def test_ambiguous_failure_with_idempotency_guarantee_is_retried(
    idempotency: IdempotencyClass,
) -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(idempotency=idempotency),
        failure_class=ToolFailureClass.AMBIGUOUS_OUTCOME,
        attempt=1,
        action_id=ACTION,
    )
    assert decision.should_retry is True
    assert decision.reason_code == "RETRY_AMBIGUOUS_IDEMPOTENT"
    assert decision.next_attempt == 2
    assert decision.delay_seconds >= 0.0


def test_transient_failure_is_retried_with_next_attempt() -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(),
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=1,
        action_id=ACTION,
    )
    assert decision.should_retry is True
    assert decision.reason_code == "RETRY_TRANSIENT_FAILURE"
    assert decision.next_attempt == 2


def test_transient_only_tool_retries_transient_failures() -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(retry=RetryClass.TRANSIENT_ONLY),
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=0,
        action_id=ACTION,
    )
    assert decision.should_retry is True
    assert decision.reason_code == "RETRY_TRANSIENT_FAILURE"
    assert decision.next_attempt == 1


def test_transient_only_tool_retries_ambiguous_idempotent_failures() -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(retry=RetryClass.TRANSIENT_ONLY, idempotency=IdempotencyClass.KEYED),
        failure_class=ToolFailureClass.AMBIGUOUS_OUTCOME,
        attempt=1,
        action_id=ACTION,
    )
    assert decision.should_retry is True
    assert decision.reason_code == "RETRY_AMBIGUOUS_IDEMPOTENT"


# --- Bounded backoff with deterministic jitter -----------------------------------


@pytest.mark.parametrize(
    ("attempt", "expected_delay"),
    [
        (0, 0.25),  # next_attempt 1: exponent clamps at 0
        (1, 0.25),  # next_attempt 2: base delay
        (2, 0.5),  # next_attempt 3: one doubling
        (3, 1.0),
        (4, 2.0),
        (5, 4.0),  # reaches the cap
        (6, 4.0),  # stays at the cap
    ],
)
def test_backoff_doubles_exponentially_and_is_capped(attempt: int, expected_delay: float) -> None:
    policy = ReliabilityPolicy(
        retry=RetrySettings(
            max_attempts=8,
            base_delay_seconds=0.25,
            max_delay_seconds=4.0,
            jitter_fraction=0.0,
        )
    )
    decision = policy.retry_decision(
        metadata=_metadata(),
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=attempt,
        action_id=ACTION,
    )
    assert decision.should_retry is True
    # base 0.25 and all doublings up to the 4.0 cap are exact in binary float
    assert decision.delay_seconds == expected_delay


def test_zero_base_delay_yields_zero_delay_even_with_full_jitter() -> None:
    policy = ReliabilityPolicy(
        retry=RetrySettings(base_delay_seconds=0.0, max_delay_seconds=0.0, jitter_fraction=1.0)
    )
    decision = policy.retry_decision(
        metadata=_metadata(),
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=1,
        action_id=ACTION,
    )
    assert decision.should_retry is True
    assert decision.delay_seconds == 0.0


def test_jitter_is_deterministic_across_policy_instances() -> None:
    first = ReliabilityPolicy().retry_decision(
        metadata=_metadata(),
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=1,
        action_id=ACTION,
    )
    second = ReliabilityPolicy().retry_decision(
        metadata=_metadata(),
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=1,
        action_id=ACTION,
    )
    assert first == second
    assert first.delay_seconds == 0.21766176617661767


def test_jitter_varies_with_action_id_and_attempt() -> None:
    policy = ReliabilityPolicy(
        retry=RetrySettings(
            max_attempts=10, base_delay_seconds=1.0, max_delay_seconds=10.0, jitter_fraction=0.5
        )
    )
    delays = {
        policy.retry_decision(
            metadata=_metadata(),
            failure_class=ToolFailureClass.TRANSIENT,
            attempt=1,
            action_id=ActionId(f"act-{index}"),
        ).delay_seconds
        for index in range(10)
    }
    assert len(delays) > 1


@given(
    base_delay_seconds=st.floats(min_value=0.0, max_value=10.0, allow_nan=False),
    extra_delay=st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
    jitter_fraction=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    attempt=st.integers(min_value=0, max_value=9),
    action_id=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        min_size=1,
        max_size=20,
    ).map(ActionId),
)
@example(
    base_delay_seconds=0.25,
    extra_delay=3.75,
    jitter_fraction=0.2,
    attempt=1,
    action_id=ACTION,
)
@example(
    base_delay_seconds=0.0,
    extra_delay=0.0,
    jitter_fraction=1.0,
    attempt=9,
    action_id=ActionId("x"),
)
def test_retry_delay_stays_within_jitter_bounds_and_is_deterministic(
    base_delay_seconds: float,
    extra_delay: float,
    jitter_fraction: float,
    attempt: int,
    action_id: ActionId,
) -> None:
    settings = RetrySettings(
        max_attempts=10,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=base_delay_seconds + extra_delay,
        jitter_fraction=jitter_fraction,
    )
    policy = ReliabilityPolicy(retry=settings)
    decision = policy.retry_decision(
        metadata=_metadata(),
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=attempt,
        action_id=action_id,
    )
    repeat = policy.retry_decision(
        metadata=_metadata(),
        failure_class=ToolFailureClass.TRANSIENT,
        attempt=attempt,
        action_id=action_id,
    )

    capped = min(
        base_delay_seconds * (2 ** max(0, attempt + 1 - 2)),
        base_delay_seconds + extra_delay,
    )
    assert decision.should_retry is True
    assert decision.next_attempt == attempt + 1
    assert decision.delay_seconds == repeat.delay_seconds
    assert decision.delay_seconds >= 0.0
    assert capped * (1.0 - jitter_fraction) - 1e-9 <= decision.delay_seconds
    assert decision.delay_seconds <= capped * (1.0 + jitter_fraction) + 1e-9


@given(attempt=st.integers(min_value=3, max_value=1000))
@example(attempt=3)
def test_no_failure_class_is_retried_once_attempts_are_exhausted(attempt: int) -> None:
    for failure_class in ToolFailureClass:
        decision = ReliabilityPolicy().retry_decision(
            metadata=_metadata(),
            failure_class=failure_class,
            attempt=attempt,
            action_id=ACTION,
        )
        assert decision.should_retry is False, failure_class.value
        assert decision.reason_code == "RETRY_ATTEMPTS_EXHAUSTED"


@given(attempt=st.integers(min_value=0, max_value=2))
@example(attempt=0)
def test_permanent_failures_are_never_retried_within_attempt_budget(attempt: int) -> None:
    decision = ReliabilityPolicy().retry_decision(
        metadata=_metadata(),
        failure_class=ToolFailureClass.PERMANENT,
        attempt=attempt,
        action_id=ACTION,
    )
    assert decision.should_retry is False
    assert decision.reason_code == "RETRY_FAILURE_PERMANENT"


# --- Circuit breaker --------------------------------------------------------------


@pytest.mark.parametrize(
    ("consecutive_failures", "expected"),
    [(0, False), (1, False), (2, False), (3, True), (4, True), (100, True)],
)
def test_circuit_opens_at_failure_threshold(consecutive_failures: int, expected: bool) -> None:
    policy = ReliabilityPolicy(circuit_failure_threshold=3)
    assert policy.circuit_is_open(consecutive_failures=consecutive_failures) is expected


def test_circuit_respects_custom_threshold() -> None:
    policy = ReliabilityPolicy(circuit_failure_threshold=1)
    assert policy.circuit_is_open(consecutive_failures=0) is False
    assert policy.circuit_is_open(consecutive_failures=1) is True


@given(
    threshold=st.integers(min_value=1, max_value=10),
    consecutive_failures=st.integers(min_value=0, max_value=20),
)
@example(threshold=3, consecutive_failures=3)
def test_circuit_opens_exactly_at_threshold(threshold: int, consecutive_failures: int) -> None:
    policy = ReliabilityPolicy(circuit_failure_threshold=threshold)
    assert policy.circuit_is_open(consecutive_failures=consecutive_failures) == (
        consecutive_failures >= threshold
    )


# --- Idempotency key derivation ----------------------------------------------------


def test_keyed_tools_derive_run_scoped_idempotency_key() -> None:
    key = idempotency_key_for(_metadata(idempotency=IdempotencyClass.KEYED), RUN, ACTION)
    assert key == "loopforge:run-1:act-1"


@pytest.mark.parametrize(
    "idempotency",
    [IdempotencyClass.NATURAL, IdempotencyClass.NOT_APPLICABLE],
)
def test_non_keyed_tools_have_no_idempotency_key(idempotency: IdempotencyClass) -> None:
    assert idempotency_key_for(_metadata(idempotency=idempotency), RUN, ACTION) is None


def test_tools_without_idempotency_guarantee_have_no_key() -> None:
    metadata = _metadata(
        retry=RetryClass.NEVER,
        idempotency=IdempotencyClass.NONE,
        side_effect=SideEffectClass.LOCAL_WRITE,
    )
    assert idempotency_key_for(metadata, RUN, ACTION) is None


def test_idempotency_keys_are_unique_per_run_and_action() -> None:
    metadata = _metadata(idempotency=IdempotencyClass.KEYED)
    keys = {idempotency_key_for(metadata, RunId(f"run-{index}"), ACTION) for index in range(5)} | {
        idempotency_key_for(metadata, RUN, ActionId(f"action-{index}")) for index in range(5)
    }
    assert None not in keys
    assert len(keys) == 10


@given(
    run_id=st.text(min_size=1, max_size=20).map(RunId),
    action_id=st.text(min_size=1, max_size=20).map(ActionId),
)
@example(run_id=RUN, action_id=ACTION)
def test_keyed_idempotency_key_embeds_run_and_action_ids(
    run_id: RunId, action_id: ActionId
) -> None:
    key = idempotency_key_for(_metadata(idempotency=IdempotencyClass.KEYED), run_id, action_id)
    assert key == f"loopforge:{run_id}:{action_id}"


def test_retry_decision_is_immutable_value() -> None:
    first = RetryDecision(True, "RETRY_TRANSIENT_FAILURE", next_attempt=2, delay_seconds=0.5)
    second = RetryDecision(True, "RETRY_TRANSIENT_FAILURE", next_attempt=2, delay_seconds=0.5)
    assert first == second
