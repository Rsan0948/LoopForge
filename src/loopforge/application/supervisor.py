"""One durable decision/action per step; no chat acknowledgement is needed."""

import hashlib

from loopforge.domain.goal_review import ControlAcknowledged, ExperimentRecorded, QuestionOpened
from loopforge.domain.goals import (
    ActionObserved,
    ActionRejected,
    CallFailed,
    CallReserved,
    DecisionRecorded,
    GoalAction,
    GoalEvent,
    GoalState,
    GoalStopped,
    ResearchRecorded,
    goal_allowed_actions,
)
from loopforge.domain.investigation import FailureKind
from loopforge.ports.clock import ClockPort
from loopforge.ports.supervisor import (
    GoalJournalPort,
    InvestigationFailure,
    InvestigationPort,
    ManagedGoalWorkerPort,
    PreparedWorkerPort,
    SupervisorFailure,
    SupervisorPort,
)


class GoalCoordinator:
    """Caller holds exclusive service ownership across each step and recovery.

    Inference is intentionally at-most-once per reservation. A restart charges
    its unknown outcome fully, then a bounded successor call may be reserved.
    Worker calls reconcile a durable decision key before retrying side effects.
    """

    def __init__(
        self,
        journal: GoalJournalPort,
        supervisor: SupervisorPort,
        worker: PreparedWorkerPort,
        clock: ClockPort,
        investigation: InvestigationPort | None = None,
    ) -> None:
        self.journal, self.supervisor, self.worker, self.clock = journal, supervisor, worker, clock
        self.investigation = investigation

    def _append(self, goal: str, sequence: int, event: GoalEvent) -> GoalState:
        # Operator events can arrive during external calls. Retry only CAS races;
        # reducer validation still rejects illegal or stale execution outcomes.
        for _ in range(16):
            try:
                return self.journal.append(goal, sequence, event)
            except ValueError:
                current = self.journal.load(goal)
                if current.sequence == sequence:
                    raise
                if (
                    isinstance(event, GoalStopped)
                    and event.status == "delivered"
                    and current.control_requested
                ):
                    event = ControlAcknowledged(current.control_requested)
                sequence = current.sequence
        msg = "Goal is receiving too many concurrent updates"
        raise ValueError(msg)

    def step(self, goal: str) -> GoalState:  # noqa: PLR0911, PLR0912
        state = self.journal.load(goal)
        if state.terminal:
            return state
        policy = state.policy
        assert policy is not None
        now = int(self.clock.now().timestamp())
        if state.phase == "inference":
            return self._append(
                goal,
                state.sequence,
                CallFailed(
                    state.reserved_microusd,
                    "Interrupted inference; outcome unknown, charged maximum",
                    now + 2 ** min(state.failures + 1, 6),
                ),
            )
        if state.control_requested:
            observation = self.worker.observe()
            if observation.status in {"queued", "coding", "reviewing"}:
                if not isinstance(self.worker, ManagedGoalWorkerPort):
                    return state  # Cannot claim stopped without a recovery capability.
                self.worker.recover_interrupted()
                observation = self.worker.observe()
                if observation.status in {"coding", "reviewing"}:
                    return state
                state = self._append(
                    goal, self.journal.load(goal).sequence, ActionObserved(observation)
                )
            return self._append(goal, state.sequence, ControlAcknowledged(state.control_requested))
        if state.paused or state.late_answer_conflict:
            if now >= policy.deadline_epoch and state.phase != "inference":
                return self._append(
                    goal,
                    state.sequence,
                    GoalStopped("blocked", "Goal deadline exhausted while waiting"),
                )
            return state
        if now >= policy.deadline_epoch:
            return self._append(
                goal, state.sequence, GoalStopped("blocked", "Goal deadline exhausted")
            )
        for topic in (*policy.clarifications, *state.dynamic_questions):
            question = next((q for q in state.questions if q.topic == topic.topic), None)
            if question is None:
                return self._append(
                    goal, state.sequence, QuestionOpened(topic.topic, now + topic.timeout_seconds)
                )
            if question.answer and topic.classification == "permission":
                return self._append(
                    goal,
                    state.sequence,
                    GoalStopped(
                        "blocked",
                        "Permission answer requires a new frozen intake; "
                        "runtime authority is unchanged",
                    ),
                )
            if not question.answer and not any(e.topic == topic.topic for e in state.experiments):
                if now >= question.deadline_epoch and topic.classification == "optional":
                    branch = (
                        "experiment/"
                        + hashlib.sha256(
                            (
                                goal
                                + topic.topic
                                + (
                                    state.last_verified_candidate
                                    if state.assignment
                                    else policy.checkpoint_revision
                                )
                            ).encode()
                        ).hexdigest()[:20]
                    )
                    return self._append(
                        goal,
                        state.sequence,
                        ExperimentRecorded(
                            topic.topic,
                            branch,
                            (state.last_verified_candidate or policy.checkpoint_revision)
                            if state.assignment
                            else policy.checkpoint_revision,
                            now,
                        ),
                    )
                # Inspection/environment work remains separately authorized. Once
                # the baseline is known (or a recovery bound is hit), wait without
                # consuming more calls for a dependent assignment.
                if (
                    not policy.investigation_enabled
                    or state.baseline_reproduced
                    or state.decisions >= policy.max_decisions
                    or state.action_rejections >= policy.max_action_rejections
                    or state.research_failures >= policy.max_research_failures
                    or state.stagnant_decisions >= policy.max_stagnant_decisions
                ):
                    return state
        if now >= policy.deadline_epoch:
            return self._append(
                goal, state.sequence, GoalStopped("blocked", "Goal deadline exhausted")
            )
        if state.phase == "action":
            return self._execute(goal, state)
        observation = self.worker.observe()
        if observation != state.observation:
            state = self._append(goal, state.sequence, ActionObserved(observation))
        if observation.status in {"queued", "coding", "reviewing"}:
            try:
                observation = self.worker.reconcile()
            except ValueError:
                return self._append(
                    goal,
                    state.sequence,
                    GoalStopped(
                        "blocked",
                        "Interrupted worker could not be reconciled; work retained",
                    ),
                )
            current = self.journal.load(goal)
            if observation == current.observation:
                return current
            return self._append(goal, current.sequence, ActionObserved(observation))
        return self._infer(goal, state, now)

    def _infer(self, goal: str, state: GoalState, now: int) -> GoalState:  # noqa: PLR0911
        policy = state.policy
        assert policy is not None
        if now < state.retry_at_epoch:
            return state
        research_exhausted = (
            state.research_failures >= policy.max_research_failures
            or state.stagnant_decisions >= policy.max_stagnant_decisions
        )
        if (
            research_exhausted
            or state.failures >= policy.max_failures
            or state.decisions >= policy.max_decisions
            or state.action_rejections >= policy.max_action_rejections
        ):
            reason = (
                "Investigation recovery or meaningful-progress allowance exhausted"
                if research_exhausted
                else "Supervisor recovery allowance exhausted"
            )
            return self._append(goal, state.sequence, GoalStopped("blocked", reason))
        try:
            request = self.supervisor.prepare(state)
        except SupervisorFailure as exc:
            return self._append(goal, state.sequence, GoalStopped("blocked", str(exc)))
        try:
            state = self._append(
                goal,
                state.sequence,
                CallReserved(
                    f"{goal}:{state.decisions + 1}",
                    request.maximum_microusd,
                    request.trace,
                ),
            )
        except ValueError:
            current = self.journal.load(goal)
            if current.control_requested or current.paused:
                return current
            return self._append(
                goal,
                current.sequence,
                GoalStopped("blocked", "Aggregate inference budget exhausted"),
            )
        try:
            response = self.supervisor.decide(request)
        except SupervisorFailure as exc:
            state = self.journal.load(goal)
            return self._append(
                goal,
                state.sequence,
                CallFailed(
                    state.reserved_microusd
                    if exc.charged_microusd is None
                    else exc.charged_microusd,
                    str(exc),
                    now + 2 ** min(state.failures + 1, 6),
                ),
            )
        state = self.journal.load(goal)
        return self._append(
            goal,
            state.sequence,
            DecisionRecorded(
                response.decision,
                response.charged_microusd,
                response.trace,
            ),
        )

    def _execute(self, goal: str, state: GoalState) -> GoalState:  # noqa: PLR0911 - explicit boundary dispatch/normalization
        decision = state.decision
        assert decision is not None
        # Always re-read actual evidence; the model cannot assert verification.
        observation = self.worker.observe()
        if decision.action not in goal_allowed_actions(state):
            return self._append(
                goal,
                state.sequence,
                ActionRejected(
                    FailureKind.PROTOCOL,
                    "Action denied by current phase; choose an allowed action.",
                ),
            )
        if decision.action == GoalAction.ASK:
            assert decision.question is not None
            assert state.policy is not None
            template = next(
                (
                    p
                    for p in state.policy.dynamic_clarifications
                    if p.topic == decision.question.category
                ),
                None,
            )
            if template is None:
                return self._append(
                    goal,
                    state.sequence,
                    ActionRejected(FailureKind.AUTHORITY, "Question category is not preauthorized"),
                )
            return self._append(
                goal,
                state.sequence,
                QuestionOpened(
                    decision.question.topic,
                    int(self.clock.now().timestamp()) + template.timeout_seconds,
                    decision.question.question,
                    decision.question.category,
                ),
            )
        research_action = decision.action in {
            GoalAction.INSPECT,
            GoalAction.PREPARE,
            GoalAction.REPRODUCE,
            GoalAction.RETRIEVE,
        }
        preparing_assignment = (
            decision.action == GoalAction.DELEGATE
            and state.policy
            and state.policy.investigation_enabled
            and (
                state.assignment is None
                or (decision.assignment is not None and decision.assignment != state.assignment)
            )
        )
        if research_action or preparing_assignment:
            return self._investigate(goal, state)
        if decision.action == GoalAction.BLOCK:
            return self._append(goal, state.sequence, GoalStopped("blocked", decision.rationale))
        if decision.action == GoalAction.DELIVER:
            if observation != state.observation:
                return self._append(
                    goal, self.journal.load(goal).sequence, ActionObserved(observation)
                )
            return self._append(goal, state.sequence, GoalStopped("delivered", decision.rationale))
        return self._run_worker(goal, state)

    def _run_worker(self, goal: str, state: GoalState) -> GoalState:
        decision = state.decision
        assert decision is not None
        if (
            decision.action == GoalAction.RETRY
            and state.policy
            and state.policy.require_recovery_evidence
        ):
            intent = decision.recovery
            known = {r.record_id for r in state.research} | set(state.observation.artifact_ids)
            duplicate = any(
                r.candidate_sha256 == state.observation.candidate_sha256
                and intent is not None
                and r.intent.changed_hypothesis.strip() == intent.changed_hypothesis.strip()
                and set(r.intent.evidence_ids) == set(intent.evidence_ids)
                for r in state.recoveries
            )
            if (
                intent is None
                or intent.failure != state.observation.failure
                or not set(intent.evidence_ids) <= known
                or duplicate
            ):
                return self._append(
                    goal,
                    state.sequence,
                    ActionRejected(
                        FailureKind.HYPOTHESIS,
                        "Retry needs the measured failure, observed evidence IDs and a concrete "
                        "changed hypothesis; unchanged retries cannot execute.",
                    ),
                )
        try:
            observation = self.worker.execute(decision.action, state.call_key)
        except ValueError:
            return self._append(
                goal,
                state.sequence,
                GoalStopped(
                    "blocked",
                    "Prepared action denied; inspect the retained local worker evidence",
                ),
            )
        return self._append(goal, self.journal.load(goal).sequence, ActionObserved(observation))

    def _investigate(self, goal: str, state: GoalState) -> GoalState:
        decision = state.decision
        assert decision is not None
        if self.investigation is None:
            return self._append(
                goal,
                state.sequence,
                GoalStopped(
                    "blocked",
                    "Investigation capability is unavailable",
                ),
            )
        try:
            record = self.investigation.perform(decision, state.call_key, state)
        except InvestigationFailure as exc:
            return self._append(goal, state.sequence, ActionRejected(exc.category, str(exc)[:700]))
        except (OSError, RuntimeError):
            return self._append(
                goal,
                state.sequence,
                ActionRejected(
                    FailureKind.ENVIRONMENT,
                    "Investigation adapter failed; inspect local environment evidence",
                ),
            )
        except ValueError as exc:
            return self._append(
                goal,
                state.sequence,
                ActionRejected(
                    FailureKind.POLICY,
                    "Investigation rejected: " + str(exc)[:700],
                ),
            )
        return self._append(goal, self.journal.load(goal).sequence, ResearchRecorded(record))
