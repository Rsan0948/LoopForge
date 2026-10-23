"""Network-free authority, durability, budget and recovery qualification."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from loopforge.adapters.goal_journal import SQLiteGoalJournal, decode_goal_event, encode_goal_event
from loopforge.application.supervisor import GoalCoordinator
from loopforge.domain.goals import (
    ActionObserved,
    CallFailed,
    CallReserved,
    DecisionRecorded,
    GoalAction,
    GoalCreated,
    GoalEvent,
    GoalPolicy,
    GoalState,
    GoalStopped,
    SupervisorDecision,
    WorkerObservation,
    reduce_goal,
)
from loopforge.domain.investigation import FailureKind, RecoveryIntent
from loopforge.ports.supervisor import SupervisorFailure, SupervisorRequest, SupervisorResponse


class Clock:
    epoch = 100

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.epoch, UTC)


class Supervisor:
    def __init__(self, actions: list[GoalAction]) -> None:
        self.actions = actions
        self.calls = 0
        self.failure: SupervisorFailure | None = None
        self.maximum = 100

    def prepare(self, state: GoalState) -> SupervisorRequest:
        assert state.policy is not None
        return SupervisorRequest("{}", self.maximum, "config")

    def decide(self, request: SupervisorRequest) -> SupervisorResponse:
        assert request.maximum_microusd == self.maximum
        self.calls += 1
        if self.failure:
            raise self.failure
        action = self.actions.pop(0)
        return SupervisorResponse(
            SupervisorDecision(action, "Evidence inspected", "Next step"), 10, "usage"
        )


class Worker:
    def __init__(self) -> None:
        self.observation = WorkerObservation()
        self.keys: dict[str, WorkerObservation] = {}
        self.calls = 0

    def observe(self) -> WorkerObservation:
        return self.observation

    def reconcile(self) -> WorkerObservation:
        return self.observe()

    def execute(self, action: GoalAction, key: str) -> WorkerObservation:
        if key in self.keys:
            return self.keys[key]
        self.calls += 1
        self.observation = (
            WorkerObservation("review_pending", "Independent coding check passed")
            if action == GoalAction.DELEGATE
            else WorkerObservation("patch_ready", "Fresh review and checks", "b" * 64, True)
        )
        self.keys[key] = self.observation
        return self.observation


@pytest.fixture
def journal(tmp_path: Path) -> SQLiteGoalJournal:
    store = SQLiteGoalJournal(tmp_path / "goals.sqlite", envelope_microusd=1000)
    store.append("g", 0, GoalCreated(GoalPolicy("Fix arithmetic", "a" * 64, 1000, 1000)))
    return store


def test_intake_to_delivery_across_restart_without_chat(journal: SQLiteGoalJournal) -> None:
    worker, clock = Worker(), Clock()
    supervisor = Supervisor([GoalAction.DELEGATE, GoalAction.VERIFY, GoalAction.DELIVER])
    coordinator = GoalCoordinator(journal, supervisor, worker, clock)
    assert coordinator.step("g").phase == "action"
    assert coordinator.step("g").observation.status == "review_pending"
    # Real journal is reopened, not an in-memory snapshot.
    reopened = SQLiteGoalJournal(journal.path, envelope_microusd=1000)
    resumed = GoalCoordinator(reopened, supervisor, worker, clock)
    result = reopened.load("g")
    for _ in range(4):
        result = resumed.step("g")
    assert result.phase == "delivered"
    assert result.spent_microusd == 30
    assert worker.calls == 2
    assert resumed.step("g") == result
    assert supervisor.calls == 3


def test_lost_worker_ack_reconciles_same_key(journal: SQLiteGoalJournal) -> None:
    worker = Worker()
    coordinator = GoalCoordinator(journal, Supervisor([GoalAction.DELEGATE]), worker, Clock())
    pending = coordinator.step("g")
    # Simulate side effect committed, process exited before ActionObserved.
    worker.execute(GoalAction.DELEGATE, pending.call_key)
    result = coordinator.step("g")
    assert result.observation.status == "review_pending"
    assert worker.calls == 1


def test_busy_worker_poll_does_not_repeat_events_or_inference(journal: SQLiteGoalJournal) -> None:
    worker, supervisor = Worker(), Supervisor([])
    worker.observation = WorkerObservation("coding", "Independent specialist is busy")
    coordinator = GoalCoordinator(journal, supervisor, worker, Clock())
    first = coordinator.step("g")
    assert first.observation.status == "coding"
    assert coordinator.step("g").sequence == first.sequence
    worker.observation = WorkerObservation("attention", "Specialist outcome recorded")
    supervisor.actions = [GoalAction.RETRY]
    assert coordinator.step("g").phase == "action"
    assert supervisor.calls == 1


def test_interrupted_inference_charged_before_successor(journal: SQLiteGoalJournal) -> None:
    state = journal.load("g")
    journal.append("g", state.sequence, CallReserved("lost", 100))
    supervisor, clock = Supervisor([GoalAction.DELEGATE]), Clock()
    coordinator = GoalCoordinator(journal, supervisor, Worker(), clock)
    result = coordinator.step("g")
    assert result.spent_microusd == 100
    assert result.reserved_microusd == 0
    assert result.failures == 1
    assert coordinator.step("g") == result
    assert supervisor.calls == 0
    clock.epoch = result.retry_at_epoch
    assert coordinator.step("g").phase == "action"
    assert supervisor.calls == 1


@pytest.mark.parametrize("charge", [None, 20])
def test_provider_failure_is_bounded_and_accounted(
    journal: SQLiteGoalJournal, charge: int | None
) -> None:
    supervisor, clock = Supervisor([]), Clock()
    supervisor.failure = SupervisorFailure("Safe failure", charge)
    coordinator = GoalCoordinator(journal, supervisor, Worker(), clock)
    for _ in range(3):
        failed = coordinator.step("g")
        clock.epoch = failed.retry_at_epoch
    result = coordinator.step("g")
    assert result.phase == "blocked"
    assert result.spent_microusd == 3 * (100 if charge is None else charge)
    assert supervisor.calls == 3


def test_budget_denial_precedes_network(journal: SQLiteGoalJournal) -> None:
    supervisor = Supervisor([])
    supervisor.maximum = 1001
    result = GoalCoordinator(journal, supervisor, Worker(), Clock()).step("g")
    assert result.phase == "blocked"
    assert supervisor.calls == 0


def test_shared_reservations_count_other_goals(journal: SQLiteGoalJournal) -> None:
    journal.append("other", 0, GoalCreated(GoalPolicy("Another goal", "a" * 64, 1000, 1000)))
    journal.append("other", 1, CallReserved("inflight", 950))
    supervisor = Supervisor([])
    assert GoalCoordinator(journal, supervisor, Worker(), Clock()).step("g").phase == "blocked"
    assert supervisor.calls == 0
    with pytest.raises(ValueError, match="immutable"):
        SQLiteGoalJournal(journal.path, envelope_microusd=999)


@pytest.mark.parametrize("action", [GoalAction.DELIVER, GoalAction.VERIFY, GoalAction.RETRY])
def test_model_cannot_expand_allowed_actions(
    journal: SQLiteGoalJournal, action: GoalAction
) -> None:
    worker = Worker()
    coordinator = GoalCoordinator(journal, Supervisor([action]), worker, Clock())
    coordinator.step("g")
    result = coordinator.step("g")
    assert result.phase == "ready"
    assert result.action_rejections == 1
    assert worker.calls == 0


def test_delivery_rechecks_candidate_and_denies_drift(journal: SQLiteGoalJournal) -> None:
    worker = Worker()
    worker.observation = WorkerObservation("patch_ready", "checks", "b" * 64, True)
    coordinator = GoalCoordinator(journal, Supervisor([GoalAction.DELIVER]), worker, Clock())
    coordinator.step("g")
    worker.observation = WorkerObservation("patch_ready", "stale", "c" * 64, False)
    result = coordinator.step("g")
    assert result.phase == "ready"
    assert result.observation.verified is False


def test_deadline_does_not_start_inference(journal: SQLiteGoalJournal) -> None:
    clock, supervisor = Clock(), Supervisor([])
    clock.epoch = 1000
    assert GoalCoordinator(journal, supervisor, Worker(), clock).step("g").phase == "blocked"
    assert supervisor.calls == 0


def test_worker_in_flight_does_not_poll_paid_model(journal: SQLiteGoalJournal) -> None:
    worker, supervisor = Worker(), Supervisor([])
    worker.observation = WorkerObservation("coding")
    assert GoalCoordinator(journal, supervisor, worker, Clock()).step("g").phase == "ready"
    assert supervisor.calls == 0


def test_terminal_history_is_immutable_and_append_only(journal: SQLiteGoalJournal) -> None:
    state = journal.append("g", 1, GoalStopped("cancelled", "operator cancelled"))
    with pytest.raises(ValueError, match="Terminal"):
        journal.append("g", state.sequence, CallReserved("revive", 1))
    with journal.connect() as db, pytest.raises(Exception, match="append only"):
        db.execute("DELETE FROM goal_events")
    assert journal.load("g") == state


def test_optimistic_concurrency_blocks_duplicate_reservation(journal: SQLiteGoalJournal) -> None:
    journal.append("g", 1, CallReserved("first", 10))
    with pytest.raises(ValueError, match="Concurrent"):
        journal.append("g", 1, CallReserved("second", 10))


@pytest.mark.parametrize(
    "event",
    [
        GoalCreated(GoalPolicy("Fix arithmetic", "a" * 64, 1000, 1000)),
        CallReserved("key", 100),
        DecisionRecorded(SupervisorDecision(GoalAction.DELEGATE, "because", "plan"), 10, "{}"),
        CallFailed(100, "unknown", 104),
        ActionObserved(WorkerObservation("attention")),
        GoalStopped("blocked", "reason"),
    ],
)
def test_strict_journal_codec_roundtrip(event: GoalEvent) -> None:
    raw = json.dumps({"kind": "reserved", "key": "key", "maximum_microusd": True})
    with pytest.raises(ValidationError):
        decode_goal_event(raw)
    assert decode_goal_event(encode_goal_event(event)) == event


def test_codec_rejects_model_authored_authority() -> None:
    with pytest.raises(ValidationError):
        decode_goal_event('{"kind":"reserved","key":"x","maximum_microusd":1,"permission":"root"}')


def test_reducer_rejects_bad_transition_and_excess_charge() -> None:
    with pytest.raises(ValueError, match="Illegal"):
        reduce_goal(GoalState(), ActionObserved(WorkerObservation()))
    policy = GoalPolicy("Fix arithmetic", "a" * 64, 1000, 1000)
    state = reduce_goal(reduce_goal(GoalState(), GoalCreated(policy)), CallReserved("x", 100))
    with pytest.raises(ValueError, match="exceeds"):
        reduce_goal(state, CallFailed(101, "bad charge", 0))
    with pytest.raises(ValueError, match="Reconcile"):
        reduce_goal(state, GoalStopped("cancelled", "stop"))
    with pytest.raises(ValueError, match="requires"):
        reduce_goal(replace(state, reserved_microusd=0), GoalStopped("delivered", "claim"))


def test_shared_envelope_is_atomic_across_concurrent_goals(journal: SQLiteGoalJournal) -> None:
    journal.append("other", 0, GoalCreated(GoalPolicy("Another goal", "a" * 64, 1000, 1000)))

    def reserve(goal: str) -> bool:
        try:
            journal.append(goal, 1, CallReserved(goal, 700))
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["g", "other"]))
    assert sorted(results) == [False, True]
    assert journal.load("g").reserved_microusd + journal.load("other").reserved_microusd == 700


def test_request_trace_is_durable_before_inference(journal: SQLiteGoalJournal) -> None:
    supervisor = Supervisor([GoalAction.DELEGATE])
    GoalCoordinator(journal, supervisor, Worker(), Clock()).step("g")
    with journal.connect() as db:
        event = decode_goal_event(
            db.execute("SELECT event FROM goal_events WHERE goal='g' AND sequence=2").fetchone()[0]
        )
    assert isinstance(event, CallReserved)
    assert event.trace == "config"


def test_repeated_key_cannot_reserve_a_second_call(journal: SQLiteGoalJournal) -> None:
    state = journal.append("g", 1, CallReserved("same", 100))
    state = journal.append("g", state.sequence, CallFailed(100, "unknown", 0))
    with pytest.raises(ValueError, match="allowance"):
        journal.append("g", state.sequence, CallReserved("same", 100))


def test_a01_rejected_phase_action_can_be_corrected_and_repeated_denials_are_bounded(
    journal: SQLiteGoalJournal,
) -> None:
    worker = Worker()
    supervisor = Supervisor([GoalAction.VERIFY, GoalAction.DELEGATE])
    coordinator = GoalCoordinator(journal, supervisor, worker, Clock())
    coordinator.step("g")
    state = coordinator.step("g")
    assert state.phase == "ready"
    assert state.action_rejections == 1
    assert worker.calls == 0
    coordinator.step("g")
    state = coordinator.step("g")
    assert worker.calls == 1
    assert state.observation.status == "review_pending"
    assert journal.load("g").action_rejections == 1


def test_a01_invalid_actions_exhaust_the_frozen_correction_allowance(
    journal: SQLiteGoalJournal,
) -> None:
    worker = Worker()
    coordinator = GoalCoordinator(journal, Supervisor([GoalAction.VERIFY] * 3), worker, Clock())
    state = journal.load("g")
    for _ in range(6):
        state = coordinator.step("g")
    assert state.action_rejections == 3
    assert not state.terminal
    assert coordinator.step("g").phase == "blocked"
    assert worker.calls == 0


@pytest.mark.parametrize("mode", ["missing", "wrong_failure", "unknown_evidence", "valid"])
def test_a13_retry_requires_measured_failure_and_a_changed_evidenced_hypothesis(
    tmp_path: Path, mode: str
) -> None:
    store = SQLiteGoalJournal(tmp_path / "goals.sqlite", envelope_microusd=1000)
    store.append(
        "g",
        0,
        GoalCreated(
            GoalPolicy(
                "Repair with observed evidence",
                "a" * 64,
                1000,
                1000,
                require_recovery_evidence=True,
            )
        ),
    )
    worker = Worker()
    worker.observation = WorkerObservation(
        "attention", "Observed failure", "b" * 64, False, FailureKind.VERIFICATION, ("e_failure",)
    )
    intent = (
        None
        if mode == "missing"
        else RecoveryIntent(
            "Replace the incorrect parser condition using the observed failure",
            ("e_unknown",) if mode == "unknown_evidence" else ("e_failure",),
            FailureKind.PROVIDER if mode == "wrong_failure" else FailureKind.VERIFICATION,
        )
    )

    class RecoverySupervisor(Supervisor):
        def decide(self, request: SupervisorRequest) -> SupervisorResponse:
            del request
            return SupervisorResponse(
                SupervisorDecision(
                    GoalAction.RETRY,
                    "Apply the evidenced correction",
                    "Use exact source and run fixed checks",
                    recovery=intent,
                ),
                10,
                "fixture",
            )

    coordinator = GoalCoordinator(store, RecoverySupervisor([]), worker, Clock())
    coordinator.step("g")
    result = coordinator.step("g")
    assert worker.calls == (1 if mode == "valid" else 0)
    assert result.action_rejections == (0 if mode == "valid" else 1)
    assert result.policy is not None
    assert result.policy.max_decisions == 12
    if mode == "valid":
        assert len(result.recoveries) == 1
        worker.observation = WorkerObservation(
            "attention",
            "Observed failure",
            "b" * 64,
            False,
            FailureKind.VERIFICATION,
            ("e_failure",),
        )
        coordinator.step("g")
        denied = coordinator.step("g")
        assert denied.action_rejections == 1
        assert worker.calls == 1
