"""The one composition of an unattended goal.

Every way of running a goal (``launch``, the console's execution service, the ``run``
commands and live qualification) builds its supervisor, recording, evidence and
runner wiring here, so a wiring fix applies to all of them. Nothing here grants
authority: budgets, scope, checks and sandbox stay in the frozen intake.
"""

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import httpx

from loopforge.adapters.gemini_supervisor import GeminiSupervisor, load_gemini_key
from loopforge.adapters.recorded_supervisor import RecordedSupervisor
from loopforge.application.supervisor import GoalCoordinator
from loopforge.domain.goals import GoalState
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL, BaseGoalService, PreparedGoalService
from loopforge.ports.supervisor import EvidenceAwareSupervisorPort

# Relative to the working directory until configuration gets an owner (migration step 2).
DEFAULT_KEY_FILE = Path(".loopforge/secrets.env")


@dataclass(frozen=True)
class GoalWiring:
    """Operator-owned runner options. ``key_file=None`` reads ``GEMINI_API_KEY``."""

    key_file: Path | None = DEFAULT_KEY_FILE
    record: bool = True
    count_tokens: bool = True
    nonblocking: bool = True


DEFAULT_WIRING = GoalWiring()


class GoalStack:
    """A goal's service, supervisor provider and coordinator, closed together."""

    def __init__(
        self, service: BaseGoalService, provider: GeminiSupervisor, coordinator: GoalCoordinator
    ) -> None:
        self.service, self.provider, self.coordinator = service, provider, coordinator

    def close(self) -> None:
        self.provider.close()

    def __enter__(self) -> "GoalStack":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def goal_service(directory: Path) -> RepositoryGoalService | PreparedGoalService:
    """The service matching the goal's frozen intake kind."""
    if (directory / "repository-intake.json").exists():
        return RepositoryGoalService(directory)
    return PreparedGoalService(directory)


def wire(
    service: BaseGoalService,
    wiring: GoalWiring = DEFAULT_WIRING,
    *,
    operator_stop: threading.Event | None = None,
    client: httpx.Client | None = None,
) -> GoalStack:
    """Compose the supervisor and coordinator for an already-open goal service.

    The caller keeps ownership of ``service``; closing the stack closes the provider.
    """
    directory = service.directory
    service.harness.nonblocking_goals = wiring.nonblocking
    service.harness.operator_stop = operator_stop
    if wiring.record:
        service.harness.provider_request_directory = directory / "worker-prompts"
    provider = GeminiSupervisor(
        load_gemini_key(wiring.key_file),
        client=client,
        count_tokens=wiring.count_tokens,
        diagnostics=directory / "supervisor-diagnostics" if wiring.record else None,
    )
    supervisor: EvidenceAwareSupervisorPort = (
        RecordedSupervisor(provider, directory / "supervisor-prompts")
        if wiring.record
        else provider
    )
    coordinator = service.coordinator(supervisor)
    if provider.evidence_store is None:
        provider.close()
        msg = "Supervisor composition lost the evidence store"
        raise ValueError(msg)
    return GoalStack(service, provider, coordinator)


def progress(state: GoalState) -> dict[str, object]:
    return {
        "sequence": state.sequence,
        "phase": state.phase,
        "decisions": state.decisions,
        "action": state.decision.action if state.decision else None,
        "worker": state.observation.status,
        "spent_microusd": state.spent_microusd,
        "summary": state.summary,
    }


def drive_goal(
    directory: Path,
    stop: threading.Event,
    wiring: GoalWiring = DEFAULT_WIRING,
    *,
    yield_when_idle: bool = False,
) -> str | None:
    """Step one goal until it is terminal, waits for a person, or ``stop`` is set.

    Returns ``"waiting"`` when the goal needs a person (paused or a conflicting late
    answer), or, with ``yield_when_idle``, when a step changed nothing and no worker is
    running, so a shared queue can serve other armed goals.
    """
    service = goal_service(directory)
    stack: GoalStack | None = None
    try:
        service.harness.nonblocking_goals = wiring.nonblocking
        service.harness.operator_stop = stop
        service.worker().recover_interrupted()
        state = service.journal.load(GOAL)
        if state.terminal:
            return None
        if state.paused or state.late_answer_conflict:
            return "waiting"
        stack = wire(service, wiring, operator_stop=stop)
        while not stop.is_set():
            before = service.journal.load(GOAL)
            state = stack.coordinator.step(GOAL)
            if state.sequence != before.sequence:
                print(json.dumps(progress(state)), flush=True)
            if state.terminal:
                return None
            if state.paused or state.late_answer_conflict:
                return "waiting"
            if (
                yield_when_idle
                and state.sequence == before.sequence
                and state.observation.status not in {"coding", "reviewing", "queued"}
            ):
                return "waiting"
            stop.wait(1)
    finally:
        if stack is not None:
            stack.close()
        service.close()
    return None
