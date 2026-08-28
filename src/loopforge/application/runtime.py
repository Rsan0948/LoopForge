from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from loopforge.application.telemetry import RuntimeTelemetry
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext, snapshot_of
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    ActionRejected,
    ArtifactRecorded,
    BudgetDebited,
    CircuitOpened,
    ContextAssembled,
    Event,
    PlanCreated,
    RetryScheduled,
    RunStarted,
    RunStopped,
    ToolExecutionStarted,
    ToolFailed,
    ToolSucceeded,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import (
    ReliabilityPolicy,
    idempotency_key_for,
)
from loopforge.domain.state import RunState, replay
from loopforge.domain.telemetry import SpanName, SpanStatusCode
from loopforge.domain.tooling import IdempotencyClass, SideEffectClass, ToolMetadata
from loopforge.domain.types import (
    ActionId,
    EventId,
    RunId,
    RunStatus,
    StopReason,
    UsageDelta,
    VerificationId,
)
from loopforge.ports.artifacts import ArtifactCollectorPort, ArtifactContractError, RunArtifact
from loopforge.ports.clock import ClockPort, SleeperPort
from loopforge.ports.context import ContextBuilderPort, ContextContractError
from loopforge.ports.model import (
    ModelContractError,
    ModelFailureClass,
    ModelPort,
    ModelTurn,
    ModelTurnError,
)
from loopforge.ports.state_store import StateStorePort
from loopforge.ports.telemetry import TelemetryPort
from loopforge.ports.tools import (
    ToolContractError,
    ToolExecutionRequest,
    ToolExecutorPort,
    ToolResult,
    UnknownToolError,
)
from loopforge.ports.verifier import VerificationResult, VerifierContractError, VerifierPort

EventFactory = Callable[[EventId, RunId, datetime, int], Event]


class UnknownRunError(LookupError):
    """Raised when resume is requested for a run with no durable event stream."""


class UnsafeResumeStateError(RuntimeError):
    """Raised when an interrupted side effect cannot be replayed safely."""


@dataclass(slots=True)
class Runtime:
    model: ModelPort
    tools: ToolExecutorPort
    verifier: VerifierPort
    store: StateStorePort
    control: ControlPolicy
    permissions: PermissionPolicy
    reliability: ReliabilityPolicy
    context: ContextBuilderPort
    clock: ClockPort
    sleeper: SleeperPort
    telemetry: TelemetryPort | None = None
    artifacts: ArtifactCollectorPort | None = None
    _telemetry: RuntimeTelemetry = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Telemetry is optional and always fail-safe: when no adapter is wired
        # the runtime emits into a no-op sink, and adapter failures can never
        # corrupt run state or change run outcomes.
        self._telemetry = RuntimeTelemetry(self.telemetry, self.clock)

    def run(self, objective: str) -> RunState:
        run_id = self.start(objective)
        return self.resume(run_id)

    def start(self, objective: str) -> RunId:
        """Create a durable, quiescent run stream without beginning tool execution."""
        run_id = RunId(f"run_{uuid4().hex[:12]}")
        self._persist(
            run_id,
            lambda event_id, rid, occurred_at, sequence: RunStarted(
                event_id=event_id,
                run_id=rid,
                occurred_at=occurred_at,
                sequence=sequence,
                objective=objective,
            ),
        )
        self._persist_plan(run_id, "Execute bounded actions until verifier passes.")
        return run_id

    def state_for(self, run_id: RunId) -> RunState:
        events = self.store.events_for(run_id)
        if not events:
            msg = f"no persisted run: {run_id}"
            raise UnknownRunError(msg)
        return replay(run_id, events)

    def cancel(self, run_id: RunId, *, summary: str = "cancelled by operator") -> RunState:
        state = self.state_for(run_id)
        if state.status.is_terminal:
            return state
        self._stop(run_id, StopReason.CANCELLED, "STOP_CANCELLED_BY_OPERATOR", summary=summary)
        return self.state_for(run_id)

    def resume(self, run_id: RunId) -> RunState:
        """Continue a persisted run using event-backed action journal semantics."""
        current = self.state_for(run_id)
        if current.status.is_terminal or current.status is RunStatus.WAITING_FOR_APPROVAL:
            return current
        if current.status is RunStatus.PLANNING:
            self._persist_plan(run_id, "Resume persisted planning state with bounded execution.")
        elif current.status is RunStatus.ACTING:
            self._resume_acting(run_id)
        elif current.status is RunStatus.VERIFYING:
            self._resolve_verifying(run_id)
        elif current.status is RunStatus.REFLECTING:
            self._persist_plan(
                run_id,
                "Resume after failed verification; choose a new bounded action.",
            )
        return self._drive(run_id)

    def _drive(self, run_id: RunId) -> RunState:  # noqa: PLR0912, PLR0915 - the drive loop is intentionally one flat orchestration of the authorized cycle; span instrumentation pushes it over the thresholds
        model_failure_streak = 0
        while True:
            current = self.state_for(run_id)
            if current.status.is_terminal:
                # A seam invoked mid-cycle (for example fail-closed artifact
                # recording after verification) may have already terminated
                # the run; never stop it twice.
                return current
            if current.status is RunStatus.REFLECTING:
                # Resume() can enter the drive loop straight from a failed
                # verification replay; the reflection plan must move the run
                # back to READY before any context assembly, exactly like the
                # post-execution path below.
                self._persist_plan(
                    run_id,
                    "Resume after failed verification; choose a new bounded action.",
                )
                current = self.state_for(run_id)
            cycle = current.iteration + 1
            self._telemetry.set_cycle(cycle)
            with self._telemetry.span(
                run_id,
                name=SpanName.CYCLE,
                attributes={"loopforge.cycle": cycle},
            ):
                with self._telemetry.span(run_id, name=SpanName.POLICY_DECISION) as policy_span:
                    decision = self.control.evaluate(current, now=self.clock.now())
                    policy_span.attributes["loopforge.policy.kind"] = decision.kind.value
                    policy_span.attributes["loopforge.policy.reason_code"] = decision.reason_code
                if decision.stop_reason is not None:
                    self._stop(run_id, decision.stop_reason, decision.reason_code)
                    return self.state_for(run_id)

                with self._telemetry.span(run_id, name=SpanName.CONTEXT_BUILD) as context_span:
                    model_context = self.context.build_context(current)
                    # Boundary validation is intentional: adapters may violate port return types.
                    if not isinstance(model_context, ModelContext):  # pyright: ignore[reportUnnecessaryIsInstance]
                        msg_12 = (
                            f"context builder returned {type(model_context).__name__}, "
                            "expected ModelContext"
                        )
                        raise ContextContractError(msg_12)
                    if model_context.run_id != run_id:
                        msg_13 = (
                            f"context builder assembled context for run {model_context.run_id}, "
                            f"expected {run_id}"
                        )
                        raise ContextContractError(msg_13)
                    context_span.attributes["loopforge.context.item_count"] = len(
                        model_context.items
                    )
                    context_span.attributes["loopforge.context.role"] = model_context.role.value
                    if model_context.prompt_template is not None:
                        context_span.attributes["loopforge.prompt.template_id"] = (
                            model_context.prompt_template.template_id
                        )
                        context_span.attributes["loopforge.prompt.template_version"] = (
                            model_context.prompt_template.version
                        )
                self._telemetry.emit_context_accounting(run_id, self.context)
                self._persist(
                    run_id,
                    lambda event_id, rid, occurred_at, sequence, ctx=model_context: (
                        ContextAssembled(
                            event_id=event_id,
                            run_id=rid,
                            occurred_at=occurred_at,
                            sequence=sequence,
                            context_items=tuple(snapshot_of(item) for item in ctx.items),
                            prompt_template_id=(
                                ctx.prompt_template.template_id
                                if ctx.prompt_template is not None
                                else None
                            ),
                            prompt_template_version=(
                                ctx.prompt_template.version
                                if ctx.prompt_template is not None
                                else None
                            ),
                        )
                    ),
                )

                try:
                    with self._telemetry.span(run_id, name=SpanName.MODEL_TURN) as model_span:
                        turn = self.model.propose_action(model_context)
                        # Boundary validation is intentional: adapters may violate port types.
                        if not isinstance(turn, ModelTurn):  # pyright: ignore[reportUnnecessaryIsInstance]
                            msg_7 = (
                                f"model adapter returned {type(turn).__name__}, expected ModelTurn"
                            )
                            raise ModelContractError(msg_7)
                        if not isinstance(turn.action, ActionProposal) or not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
                            turn.usage, UsageDelta
                        ):
                            msg_8 = "model turn contains invalid action or usage payload"
                            raise ModelContractError(msg_8)
                        model_span.attributes["loopforge.usage.cost_usd"] = turn.usage.cost_usd
                        model_span.attributes["loopforge.usage.input_tokens"] = (
                            turn.usage.input_tokens
                        )
                        model_span.attributes["loopforge.usage.output_tokens"] = (
                            turn.usage.output_tokens
                        )
                        model_span.attributes["loopforge.usage.cached_input_tokens"] = (
                            turn.usage.cached_input_tokens
                        )
                except ModelTurnError as error:
                    # Provider failures arrive normalized and classified by the
                    # adapter; the runtime owns the stopping decision. Permanent
                    # failures stop the run explicitly; transient failures retry
                    # with bounded backoff inside the loop (a retry never
                    # persists an action, so it cannot burn max_iterations, and
                    # a crash mid-backoff resumes safely from READY). Adapter
                    # text is bounded at this boundary before it can enter the
                    # durable stream.
                    stop_summary = _bounded_stop_text(error.reason_code, error.summary)
                    if error.failure_class is ModelFailureClass.PERMANENT:
                        self._stop(
                            run_id,
                            StopReason.FAILURE,
                            error.reason_code,
                            summary=stop_summary,
                        )
                        return self.state_for(run_id)
                    model_failure_streak += 1
                    if model_failure_streak >= self.reliability.retry.max_attempts:
                        self._stop(
                            run_id,
                            StopReason.FAILURE,
                            error.reason_code,
                            summary=stop_summary,
                        )
                        return self.state_for(run_id)
                    backoff = min(
                        self.reliability.retry.base_delay_seconds * 2 ** (model_failure_streak - 1),
                        self.reliability.retry.max_delay_seconds,
                    )
                    self.sleeper.sleep(backoff)
                    continue
                model_failure_streak = 0
                self._persist(
                    run_id,
                    lambda event_id, rid, occurred_at, sequence, usage=turn.usage: BudgetDebited(
                        event_id=event_id,
                        run_id=rid,
                        occurred_at=occurred_at,
                        sequence=sequence,
                        usage=usage,
                    ),
                )

                after_debit = self.state_for(run_id)
                with self._telemetry.span(run_id, name=SpanName.POLICY_DECISION) as budget_span:
                    budget_decision = self.control.evaluate(after_debit, now=self.clock.now())
                    budget_span.attributes["loopforge.policy.kind"] = budget_decision.kind.value
                    budget_span.attributes["loopforge.policy.reason_code"] = (
                        budget_decision.reason_code
                    )
                if budget_decision.stop_reason is not None:
                    self._stop(run_id, budget_decision.stop_reason, budget_decision.reason_code)
                    return self.state_for(run_id)

                action = turn.action
                self._persist(
                    run_id,
                    lambda event_id, rid, occurred_at, sequence, action=action: ActionProposed(
                        event_id=event_id,
                        run_id=rid,
                        occurred_at=occurred_at,
                        sequence=sequence,
                        proposal=action,
                    ),
                )

                try:
                    metadata = self.tools.metadata_for(action.tool_name)
                    if not isinstance(metadata, ToolMetadata):  # pyright: ignore[reportUnnecessaryIsInstance]
                        msg_11 = (
                            f"tool metadata adapter returned {type(metadata).__name__}, "
                            "expected ToolMetadata"
                        )
                        raise ToolContractError(msg_11)
                except UnknownToolError:
                    self._reject(run_id, action, "BLOCK_UNKNOWN_TOOL")
                    continue

                current = self.state_for(run_id)
                if metadata.name in current.open_circuit_tools:
                    self._reject(run_id, action, "BLOCK_CIRCUIT_OPEN")
                    continue
                if not self.permissions.authorizes(metadata):
                    self._reject(run_id, action, "BLOCK_PERMISSION_DENIED")
                    continue

                self._persist(
                    run_id,
                    lambda event_id, rid, occurred_at, sequence, action=action, metadata=metadata: (
                        ActionAuthorized(
                            event_id=event_id,
                            run_id=rid,
                            occurred_at=occurred_at,
                            sequence=sequence,
                            proposal=action,
                            tool_metadata=metadata,
                        )
                    ),
                )
                self._execute_current_action(run_id, attempt=1)
                self._resolve_verifying(run_id)

                verification_state = self.state_for(run_id)
                if verification_state.status is RunStatus.REFLECTING:
                    self._persist_plan(
                        run_id,
                        "Previous verification failed; choose a new bounded action.",
                    )

    def _resume_acting(self, run_id: RunId) -> None:
        state = self.state_for(run_id)
        proposal = state.current_proposal
        metadata = state.current_tool_metadata
        if proposal is None or metadata is None:
            msg_2 = "acting state is missing durable action metadata"
            raise UnsafeResumeStateError(msg_2)

        if state.execution_in_flight:
            safe = metadata.side_effect in {SideEffectClass.PURE, SideEffectClass.READ_ONLY} or (
                metadata.idempotency in {IdempotencyClass.NATURAL, IdempotencyClass.KEYED}
            )
            if not safe:
                msg_9 = "ambiguous action outcome cannot be replayed without idempotency"
                raise UnsafeResumeStateError(msg_9)
            self._execute_request(
                run_id,
                proposal=proposal,
                attempt=max(1, state.current_attempt),
                idempotency_key=state.current_idempotency_key,
                record_start=False,
            )
        else:
            if state.retry_not_before is not None:
                remaining = (state.retry_not_before - self.clock.now()).total_seconds()
                if remaining > 0:
                    self.sleeper.sleep(remaining)
            self._execute_current_action(run_id, attempt=max(1, state.current_attempt + 1))
        self._resolve_verifying(run_id)

    def _execute_current_action(self, run_id: RunId, *, attempt: int) -> None:
        state = self.state_for(run_id)
        proposal = state.current_proposal
        metadata = state.current_tool_metadata
        if proposal is None or metadata is None:
            msg_3 = "authorized action metadata missing from state"
            raise RuntimeError(msg_3)
        self._execute_request(
            run_id,
            proposal=proposal,
            attempt=attempt,
            idempotency_key=idempotency_key_for(metadata, run_id, proposal.action_id),
            record_start=True,
        )

    def _execute_request(
        self,
        run_id: RunId,
        *,
        proposal: ActionProposal,
        attempt: int,
        idempotency_key: str | None,
        record_start: bool,
    ) -> None:
        if record_start:
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence: ToolExecutionStarted(
                    event_id=event_id,
                    run_id=rid,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    action_id=proposal.action_id,
                    attempt=attempt,
                    idempotency_key=idempotency_key,
                ),
            )
        metadata = self.state_for(run_id).current_tool_metadata
        if metadata is None:
            msg_4 = "tool execution missing metadata"
            raise RuntimeError(msg_4)
        with self._telemetry.span(
            run_id,
            name=SpanName.TOOL_EXECUTE,
            action_id=proposal.action_id,
            tool_name=metadata.name,
            attempt=attempt,
            attributes={"loopforge.tool.timeout_seconds": metadata.timeout_seconds},
        ) as tool_span:
            if idempotency_key is not None:
                tool_span.attributes["loopforge.tool.idempotency_key"] = idempotency_key
            result = self.tools.execute(
                ToolExecutionRequest(
                    proposal=proposal,
                    attempt=attempt,
                    timeout_seconds=metadata.timeout_seconds,
                    idempotency_key=idempotency_key,
                )
            )
            # Boundary validation is intentional: adapters may violate port return types.
            if not isinstance(result, ToolResult):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg_5 = f"tool adapter returned {type(result).__name__}, expected ToolResult"
                raise ToolContractError(msg_5)
            tool_span.attributes["loopforge.tool.ok"] = result.ok
            if not result.ok:
                tool_span.status = SpanStatusCode.ERROR
        self._record_tool_result(run_id, proposal.action_id, attempt, result)

    def _record_tool_result(
        self,
        run_id: RunId,
        action_id: ActionId,
        attempt: int,
        result: ToolResult,
    ) -> None:
        if result.ok:
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence: ToolSucceeded(
                    event_id=event_id,
                    run_id=rid,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    action_id=action_id,
                    observation=result.observation,
                    attempt=attempt,
                ),
            )
            return
        failure_class = result.failure_class
        if failure_class is None:
            msg_6 = "failed tool result missing failure classification"
            raise RuntimeError(msg_6)
        self._persist(
            run_id,
            lambda event_id, rid, occurred_at, sequence: ToolFailed(
                event_id=event_id,
                run_id=rid,
                occurred_at=occurred_at,
                sequence=sequence,
                action_id=action_id,
                error_code=result.error_code or "UNKNOWN",
                error_message=result.observation,
                failure_class=failure_class,
                attempt=attempt,
            ),
        )

    def _resolve_verifying(self, run_id: RunId) -> None:
        state = self.state_for(run_id)
        if state.status is not RunStatus.VERIFYING:
            return
        if state.last_tool_failure_class is not None:
            metadata = state.current_tool_metadata
            proposal = state.current_proposal
            if metadata is None or proposal is None:
                msg_10 = "tool failure missing action metadata"
                raise RuntimeError(msg_10)

            streak = state.failure_streak_for(metadata.name)
            if self.reliability.circuit_is_open(consecutive_failures=streak):
                if metadata.name not in state.open_circuit_tools:
                    self._persist(
                        run_id,
                        lambda event_id, rid, occurred_at, sequence: CircuitOpened(
                            event_id=event_id,
                            run_id=rid,
                            occurred_at=occurred_at,
                            sequence=sequence,
                            tool_name=metadata.name,
                            reason_code="CIRCUIT_OPEN_CONSECUTIVE_FAILURES",
                        ),
                    )
                self._verify_and_record(run_id)
                return

            retry = self.reliability.retry_decision(
                metadata=metadata,
                failure_class=state.last_tool_failure_class,
                attempt=max(1, state.current_attempt),
                action_id=proposal.action_id,
            )
            if retry.should_retry:
                assert retry.next_attempt is not None
                next_attempt = retry.next_attempt
                with self._telemetry.span(
                    run_id,
                    name=SpanName.RETRY,
                    action_id=proposal.action_id,
                    tool_name=metadata.name,
                    attributes={
                        "loopforge.retry.next_attempt": next_attempt,
                        "loopforge.retry.delay_seconds": retry.delay_seconds,
                        "loopforge.retry.reason_code": retry.reason_code,
                    },
                ):
                    self._persist(
                        run_id,
                        lambda event_id, rid, occurred_at, sequence: RetryScheduled(
                            event_id=event_id,
                            run_id=rid,
                            occurred_at=occurred_at,
                            sequence=sequence,
                            action_id=proposal.action_id,
                            next_attempt=next_attempt,
                            delay_seconds=retry.delay_seconds,
                            reason_code=retry.reason_code,
                        ),
                    )
                    self.sleeper.sleep(retry.delay_seconds)
                    self._execute_current_action(run_id, attempt=next_attempt)
                    self._resolve_verifying(run_id)
                return

        self._verify_and_record(run_id)

    def _verify_and_record(self, run_id: RunId) -> None:
        # The verification correlation id derives from the sequence the
        # verification event will be durably appended with, tying telemetry
        # causally to the authoritative history.
        verification_id = VerificationId(
            f"{run_id}:verification:{self.store.current_version(run_id) + 1}"
        )
        with self._telemetry.span(
            run_id, name=SpanName.VERIFY, verification_id=verification_id
        ) as verify_span:
            verification = self.verifier.verify(self.state_for(run_id))
            # Boundary validation is intentional: adapters may violate port
            # return types, and a malformed verdict must never be persisted.
            if not isinstance(verification, VerificationResult):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg_15 = (
                    f"verifier returned {type(verification).__name__}, expected VerificationResult"
                )
                raise VerifierContractError(msg_15)
            verify_span.attributes["loopforge.verification.passed"] = verification.passed
            if verification.score is not None:
                verify_span.attributes["loopforge.verification.score"] = verification.score
        if verification.passed:
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence: VerificationPassed(
                    event_id=event_id,
                    run_id=rid,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    summary=verification.summary,
                ),
            )
        else:
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence: VerificationFailed(
                    event_id=event_id,
                    run_id=rid,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    summary=verification.summary,
                    score=verification.score,
                ),
            )
        self._record_artifacts(run_id)

    def _record_artifacts(self, run_id: RunId) -> None:
        """Persist workload-supplied evidence artifacts after verification.

        The collector is an optional, workload-agnostic seam: the runtime core
        has no knowledge of workspaces or patches, it only durably records
        whatever typed artifacts the bound workload supplies. Evidence
        collection is fail-closed: a collector failure terminates the run as
        ``FAILURE`` with the reason recorded, so a run can never wedge
        non-terminal (and re-raise on every resume) because evidence could not
        be gathered. Byte-identical re-records (same kind, label, and content
        as an already-recorded artifact) are skipped, keeping evidence
        at-most-once across crash/resume between artifact appends while never
        dropping fresh per-cycle evidence.
        """
        if self.artifacts is None:
            return
        state = self.state_for(run_id)
        try:
            collected = self.artifacts.collect(state)
            # Validate the whole batch before persisting anything: contract
            # violations must not leave partial evidence behind.
            validated = tuple(self._validate_artifact(item) for item in collected)
        except Exception as exc:
            self._stop(
                run_id,
                StopReason.FAILURE,
                "ARTIFACT_COLLECTION_FAILED",
                summary=f"artifact collection failed: {type(exc).__name__}: {exc}",
            )
            return
        known = {
            (fingerprint.kind, fingerprint.label, fingerprint.content)
            for fingerprint in state.recorded_artifacts
        }
        for artifact in validated:
            if (artifact.kind.value, artifact.label, artifact.content) in known:
                continue
            self._persist(
                run_id,
                lambda event_id, rid, occurred_at, sequence, artifact=artifact: ArtifactRecorded(
                    event_id=event_id,
                    run_id=rid,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    kind=artifact.kind,
                    label=artifact.label,
                    content=artifact.content,
                ),
            )

    @staticmethod
    def _validate_artifact(artifact: object) -> RunArtifact:
        # Boundary validation is intentional: adapters may violate port return types.
        if not isinstance(artifact, RunArtifact):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_14 = f"artifact collector returned {type(artifact).__name__}, expected RunArtifact"
            raise ArtifactContractError(msg_14)
        return artifact

    def _persist_plan(self, run_id: RunId, plan: str) -> None:
        self._persist(
            run_id,
            lambda event_id, rid, occurred_at, sequence: PlanCreated(
                event_id=event_id,
                run_id=rid,
                occurred_at=occurred_at,
                sequence=sequence,
                plan=plan,
            ),
        )

    def _reject(self, run_id: RunId, action: ActionProposal, reason_code: str) -> None:
        self._persist(
            run_id,
            lambda event_id, rid, occurred_at, sequence: ActionRejected(
                event_id=event_id,
                run_id=rid,
                occurred_at=occurred_at,
                sequence=sequence,
                proposal=action,
                reason_code=reason_code,
            ),
        )

    def _stop(
        self,
        run_id: RunId,
        reason: StopReason,
        reason_code: str,
        *,
        summary: str | None = None,
    ) -> None:
        self._persist(
            run_id,
            lambda event_id, rid, occurred_at, sequence: RunStopped(
                event_id=event_id,
                run_id=rid,
                occurred_at=occurred_at,
                sequence=sequence,
                reason=reason,
                summary=summary or reason_code,
            ),
        )
        final = self.state_for(run_id)
        self._telemetry.emit_run_span(
            run_id,
            started_at=final.started_at or self.clock.now(),
            reason=reason,
            attributes={
                "loopforge.run.outcome": reason.value,
                "loopforge.run.iterations": final.iteration,
                "loopforge.run.cost_usd": final.cost_usd,
            },
        )

    def _persist(self, run_id: RunId, factory: EventFactory) -> None:
        expected_version = self.store.current_version(run_id)
        event = factory(
            EventId(f"evt_{uuid4().hex[:16]}"),
            run_id,
            self.clock.now(),
            expected_version + 1,
        )
        with self._telemetry.span(
            run_id,
            name=SpanName.PERSIST,
            attributes={
                "loopforge.event.type": type(event).__name__,
                "loopforge.event.sequence": event.sequence,
            },
        ):
            self.store.append(event, expected_version=expected_version)
        # Telemetry is projected only after the authoritative event is durable;
        # it never feeds back into runtime state or decisions.
        self._telemetry.project_event(event)


_STOP_TEXT_BUDGET = 500


def _bounded_stop_text(reason_code: str, summary: str) -> str:
    """Bound and sanitize adapter-supplied text before it enters a durable event.

    ``ModelTurnError`` text crosses the port boundary from adapters the runtime
    cannot fully trust to self-bound; control characters must never forge
    lines in downstream consumers of the authoritative stream.
    """
    combined = f"{reason_code}: {summary}"
    sanitized = "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in combined)
    if len(sanitized) <= _STOP_TEXT_BUDGET:
        return sanitized
    return sanitized[: _STOP_TEXT_BUDGET - 3] + "..."
