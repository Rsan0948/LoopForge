"""Review events may annotate terminal results, never reactivate terminal runs."""

from dataclasses import replace
from typing import TYPE_CHECKING

from loopforge.domain.goal_review import (
    ClarificationPolicy,
    ControlAcknowledged,
    ControlRequested,
    ExperimentRecorded,
    ExperimentState,
    QuestionAnswered,
    QuestionOpened,
    QuestionState,
    ReviewRecorded,
)

if TYPE_CHECKING:
    from loopforge.domain.goals import GoalEvent, GoalState


def reduce_review(state: "GoalState", event: "GoalEvent") -> "GoalState | None":  # noqa: PLR0911, PLR0912, PLR0915 - explicit boundary dispatch/normalization
    policy = state.policy
    if policy is None:
        return None
    topics = {p.topic: p for p in (*policy.clarifications, *state.dynamic_questions)}
    questions = {q.topic: q for q in state.questions}
    if isinstance(event, QuestionOpened):
        dynamic = state.dynamic_questions
        if event.category:
            template = next(
                (p for p in policy.dynamic_clarifications if p.topic == event.category), None
            )
            decision = state.decision
            if (
                template is None
                or len(dynamic) >= 8
                or decision is None
                or decision.question is None
                or decision.question.topic != event.topic
                or decision.question.category != event.category
                or decision.question.question != event.question
                or event.topic in topics
            ):
                msg = "Dynamic question must match a frozen category and current ask decision"
                raise ValueError(msg)
            entry = ClarificationPolicy(
                event.topic,
                event.question,
                template.classification,
                template.timeout_seconds,
                template.fallback,
            )
            dynamic = (*dynamic, entry)
            topics[event.topic] = entry
        if (
            state.terminal
            or event.topic not in topics
            or event.topic in questions
            or type(event.deadline_epoch) is not int
            or event.deadline_epoch <= 0
        ):
            msg = "Question must match unused frozen clarification policy"
            raise ValueError(msg)
        return replace(
            state,
            questions=(*state.questions, QuestionState(event.topic, event.deadline_epoch)),
            dynamic_questions=dynamic,
            phase="ready",
            decision=None,
        )
    if isinstance(event, QuestionAnswered):
        if (
            event.topic not in questions
            or not event.answer.strip()
            or len(event.answer) > 2000
            or type(event.answered_epoch) is not int
            or event.answered_epoch <= 0
        ):
            msg = "Answer requires a known question and bounded operator text"
            raise ValueError(msg)
        if questions[event.topic].answer:
            msg = "Question already answered; retain original answer"
            raise ValueError(msg)
        conflict = any(
            e.topic == event.topic and e.assumption != event.answer for e in state.experiments
        )
        return replace(
            state,
            questions=tuple(
                replace(q, answer=event.answer, answered_epoch=event.answered_epoch)
                if q.topic == event.topic
                else q
                for q in state.questions
            ),
            late_answer_conflict=state.late_answer_conflict or conflict,
            control_requested="cancel"
            if state.control_requested == "cancel"
            else "pause"
            if not state.terminal and (conflict or state.phase in {"inference", "action"})
            else state.control_requested,
        )
    if isinstance(event, ExperimentRecorded):
        q = questions.get(event.topic)
        p = topics.get(event.topic)
        if (
            state.terminal
            or q is None
            or p is None
            or q.answer
            or p.classification != "optional"
            or type(event.recorded_epoch) is not int
            or event.recorded_epoch < q.deadline_epoch
            or event.checkpoint
            != (
                (state.last_verified_candidate or policy.checkpoint_revision)
                if state.assignment
                else policy.checkpoint_revision
            )
            or any(e.topic == event.topic for e in state.experiments)
            or not event.branch.startswith("experiment/")
            or len(event.branch) != len("experiment/") + 20
            or any(c not in "0123456789abcdef" for c in event.branch.removeprefix("experiment/"))
        ):
            msg = (
                "Experiment requires expired preauthorized optional fallback and pinned checkpoint"
            )
            raise ValueError(msg)
        return replace(
            state,
            experiments=(
                *state.experiments,
                ExperimentState(event.topic, event.branch, event.checkpoint, p.fallback),
            ),
        )
    if isinstance(event, ControlRequested):
        if state.terminal or event.command not in {"pause", "cancel", "resume"}:
            msg = "Invalid control request or terminal goal"
            raise ValueError(msg)
        if event.command == "resume":
            if not state.paused or state.late_answer_conflict:
                msg = "Resume requires acknowledged pause without conflicting late answer"
                raise ValueError(msg)
            return replace(state, paused=False, control_requested="")
        if state.control_requested == "cancel" and event.command != "cancel":
            msg = "Cancellation cannot be downgraded"
            raise ValueError(msg)
        return replace(state, control_requested=event.command)
    if isinstance(event, ControlAcknowledged):
        if (
            state.terminal
            or state.phase == "inference"
            or event.command != state.control_requested
            or event.command not in {"pause", "cancel"}
        ):
            msg = "Control acknowledgement requires reconciled in-flight work"
            raise ValueError(msg)
        return replace(
            state,
            phase="cancelled" if event.command == "cancel" else "ready",
            paused=event.command == "pause",
            control_requested="",
            decision=None,
            summary="Cancelled by operator; work retained"
            if event.command == "cancel"
            else "Paused by operator; work retained",
        )
    if isinstance(event, ReviewRecorded):
        if (
            not state.terminal
            or event.disposition not in {"accepted", "discarded"}
            or state.disposition != "pending"
        ):
            msg = "Review requires an undisposed terminal candidate"
            raise ValueError(msg)
        if event.disposition == "accepted" and (
            state.phase != "delivered"
            or state.late_answer_conflict
            or not state.observation.verified
            or event.candidate_sha256 != state.observation.candidate_sha256
            or len(event.commit) != 40
            or any(c not in "0123456789abcdef" for c in event.commit)
        ):
            msg = "Accept requires unchanged verified delivery without conflicting answer"
            raise ValueError(msg)
        return replace(state, disposition=event.disposition, accepted_commit=event.commit)
    return None
