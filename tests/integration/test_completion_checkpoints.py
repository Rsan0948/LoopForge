"""Passing checks are not whole-task completion; recovery preserves that distinction."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.json_events import JsonEventCodec, UnsupportedEventSchemaError
from loopforge.adapters.scripted import (
    FixedClock,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
    ScriptedVerifier,
)
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.application.counterfactual import extract_scripted_turns
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import VerificationFailed, VerificationPassed
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.state import replay
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
    StopReason,
)
from loopforge.ports.tools import ToolResult
from loopforge.ports.verifier import VerificationResult

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def runtime(
    store: SQLiteEventStore, verdicts: list[VerificationResult], *, prefix: str = "part"
) -> Runtime:
    clock = FixedClock(NOW)
    return Runtime(
        model=ScriptedModel(
            [
                ActionProposal(ActionId(f"{prefix}-{i}"), "implement", {})
                for i in range(len(verdicts))
            ]
        ),
        tools=ScriptedTools(
            [ToolResult(ok=True, observation="worker claims success") for _ in verdicts],
            metadata=[
                ToolMetadata(
                    name="implement",
                    risk=RiskLevel.LOCAL_WRITE,
                    required_permission=Permission.LOCAL_WRITE,
                    side_effect=SideEffectClass.LOCAL_WRITE,
                    retry=RetryClass.NEVER,
                    idempotency=IdempotencyClass.NONE,
                    approval=ApprovalClass.NONE,
                    timeout_seconds=5,
                )
            ],
        ),
        verifier=ScriptedVerifier(verdicts),
        store=store,
        control=ControlPolicy(BudgetLimit(max_cost_usd=5, max_iterations=10)),
        permissions=PermissionPolicy(frozenset({Permission.LOCAL_WRITE})),
        reliability=ReliabilityPolicy(),
        context=BasicContextBuilder(clock),
        clock=clock,
        sleeper=RecordingSleeper(),
    )


def test_passing_checkpoint_survives_restart_and_does_not_end_multi_part_task(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(path, codec=JsonEventCodec())
    first = runtime(
        store, [VerificationResult(True, "first criterion passed", task_complete=False)]
    )
    run_id = first.start("Implement two independently verified outcomes")
    checkpoint = first.step(run_id)
    assert checkpoint.last_verification_passed is True
    assert checkpoint.last_verification_task_complete is False
    assert not checkpoint.status.is_terminal
    events = store.events_for(run_id)
    assert extract_scripted_turns(events).verifications[0].task_complete is False
    assert replay(run_id, events) == checkpoint
    assert store.list_runs()[0].status == checkpoint.status

    reopened = SQLiteEventStore(path, codec=JsonEventCodec())
    second = runtime(reopened, [VerificationResult(True, "both criteria verified")], prefix="final")
    completed = second.resume(run_id)
    assert completed.stop_reason is StopReason.SUCCESS_VERIFIED
    assert completed.iteration == 2
    assert completed.last_verification_task_complete is True
    assert len([e for e in reopened.events_for(run_id) if isinstance(e, VerificationPassed)]) == 2


@pytest.mark.parametrize(
    "failure", ["repeated_checkpoint", "exhausted_budget", "failed_acceptance"]
)
def test_checkpoint_cannot_manufacture_completion(tmp_path: Path, failure: str) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite", codec=JsonEventCodec())
    verdicts = [VerificationResult(True, "subset passes", task_complete=False)] * 4
    if failure == "failed_acceptance":
        verdicts = [verdicts[0], VerificationResult(False, "independent acceptance fails")]
    instance = runtime(store, verdicts)
    if failure == "exhausted_budget":
        instance.control = ControlPolicy(BudgetLimit(max_cost_usd=0.01, max_iterations=10))
    elif failure == "failed_acceptance":
        instance.control = ControlPolicy(BudgetLimit(max_cost_usd=5, max_iterations=len(verdicts)))
    result = instance.resume(instance.start("All outcomes must pass"))
    assert result.stop_reason is not StopReason.SUCCESS_VERIFIED
    assert result.status.is_terminal
    if failure == "repeated_checkpoint":
        assert result.stop_reason is StopReason.STALLED
    elif failure == "exhausted_budget":
        assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    else:
        assert result.stop_reason is StopReason.MAX_ITERATIONS


@pytest.mark.parametrize("invalid", [None, "true", 1, [], {}])
def test_completion_verdict_rejects_non_boolean_boundary_values(invalid: object) -> None:
    with pytest.raises(TypeError, match="task_complete"):
        VerificationResult(True, "checks", task_complete=invalid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="task_complete"):
        VerificationPassed(
            event_id=EventId("invalid"),
            run_id=RunId("invalid"),
            occurred_at=NOW,
            sequence=1,
            summary="checks",
            task_complete=invalid,  # type: ignore[arg-type]
        )


def test_legacy_verification_events_keep_whole_task_semantics() -> None:
    event = VerificationPassed(
        event_id=EventId("legacy"),
        run_id=RunId("legacy"),
        occurred_at=NOW,
        sequence=1,
        summary="all criteria pass",
    )
    codec = JsonEventCodec()
    payload = json.loads(codec.encode(event))
    del payload["event"]["task_complete"]
    del payload["event"]["score"]
    assert codec.decode(json.dumps(payload)) == event
    payload["event"]["task_complete"] = "false"
    with pytest.raises(TypeError, match="task_complete"):
        codec.decode(json.dumps(payload))


def test_distinct_verified_progress_can_continue_beyond_the_stall_threshold(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite", codec=JsonEventCodec())
    verdicts = [
        VerificationResult(True, f"criterion {i} verified", score=i / 6, task_complete=False)
        for i in range(1, 6)
    ] + [VerificationResult(True, "all six criteria verified")]
    instance = runtime(store, verdicts)
    result = instance.resume(instance.start("Verify six independently assessed criteria"))
    assert result.stop_reason is StopReason.SUCCESS_VERIFIED
    assert result.iteration == 6
    checkpoints = [e for e in store.events_for(result.run_id) if isinstance(e, VerificationPassed)]
    assert [event.score for event in checkpoints] == [1 / 6, 2 / 6, 3 / 6, 4 / 6, 5 / 6, 1]


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf"), True, "0.5"])
def test_checkpoint_event_rejects_invalid_progress_score(score: object) -> None:
    with pytest.raises(ValueError, match="score"):
        VerificationPassed(
            event_id=EventId("score"),
            run_id=RunId("score"),
            occurred_at=NOW,
            sequence=1,
            summary="checkpoint",
            task_complete=False,
            score=score,  # type: ignore[arg-type]
        )


def test_checkpoint_wire_version_fails_closed_for_legacy_readers() -> None:
    codec = JsonEventCodec()
    checkpoint = VerificationPassed(
        event_id=EventId("checkpoint"),
        run_id=RunId("checkpoint"),
        occurred_at=NOW,
        sequence=1,
        summary="partial",
        task_complete=False,
        score=0.5,
    )
    encoded = codec.encode(checkpoint)
    payload = json.loads(encoded)
    assert payload["schema_version"] == 2
    assert codec.decode(encoded) == checkpoint
    # Legacy readers only accept schema v1. The new reader also refuses a
    # downgrade: ignoring task_complete must never turn this into completion.
    payload["schema_version"] = 1
    with pytest.raises(UnsupportedEventSchemaError, match="checkpoints require"):
        codec.decode(json.dumps(payload))
    payload["schema_version"] = 2
    payload["event"]["task_complete"] = True
    with pytest.raises(UnsupportedEventSchemaError, match="checkpoints require"):
        codec.decode(json.dumps(payload))


def test_restart_at_checkpoint_event_before_next_plan_keeps_projection_consistent(
    tmp_path: Path,
) -> None:
    store = SQLiteEventStore(tmp_path / "original.sqlite", codec=JsonEventCodec())
    first = runtime(
        store, [VerificationResult(True, "part verified", score=0.5, task_complete=False)]
    )
    run_id = first.start("Complete both parts")
    first.step(run_id)
    interrupted = SQLiteEventStore(tmp_path / "interrupted.sqlite", codec=JsonEventCodec())
    for event in store.events_for(run_id):
        interrupted.append(event, expected_version=event.sequence - 1)
        if isinstance(event, VerificationPassed):
            break
    projection = replay(run_id, interrupted.events_for(run_id))
    assert interrupted.list_runs()[0].status == projection.status
    assert projection.last_verification_task_complete is False
    recovered = runtime(
        interrupted, [VerificationResult(True, "both parts verified")], prefix="final"
    )
    result = recovered.resume(run_id)
    assert result.stop_reason is StopReason.SUCCESS_VERIFIED
    assert result.iteration == 2


def test_counterfactual_preserves_inconclusive_checkpoint_failure() -> None:
    failure = VerificationFailed(
        event_id=EventId("unavailable"),
        run_id=RunId("unavailable"),
        occurred_at=NOW,
        sequence=1,
        summary="acceptance command did not execute",
        inconclusive=True,
    )
    result = extract_scripted_turns((failure,)).verifications[0]
    assert result.passed is False
    assert result.inconclusive is True


@pytest.mark.parametrize("invalid", [True, False, "0.5", [], {}])
def test_verifier_rejects_invalid_checkpoint_progress_before_runtime(invalid: object) -> None:
    with pytest.raises(ValueError, match="finite fraction"):
        VerificationResult(True, "partial", task_complete=False, score=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", [], {}])
def test_verifier_rejects_non_boolean_inconclusive_flag(invalid: object) -> None:
    with pytest.raises(TypeError, match="inconclusive must be a bool"):
        VerificationResult(False, "unmeasured", inconclusive=invalid)  # type: ignore[arg-type]
