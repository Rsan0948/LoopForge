from __future__ import annotations

from loopforge.domain.context import ContextItem, ContextSource, ModelContext
from loopforge.domain.security import TrustClass
from loopforge.domain.state import RunState
from loopforge.domain.tooling import DataSensitivity
from loopforge.domain.types import ContextItemId
from loopforge.ports.clock import ClockPort


class BasicContextBuilder:
    """Deterministic context assembly directly from the run projection.

    Trust classes are assigned by the runtime, never by content: the operator
    objective is authorized-human input, the runtime plan is runtime policy,
    and tool/verifier feedback is deterministic observation. Selection,
    budgeting, and compaction are deliberately out of scope for this cycle.
    """

    def __init__(self, clock: ClockPort) -> None:
        self._clock = clock

    def build_context(self, state: RunState) -> ModelContext:
        now = self._clock.now()
        items: list[ContextItem] = []

        def item(key: str, content: str, trust: TrustClass, detail: str) -> ContextItem:
            return ContextItem(
                item_id=ContextItemId(f"{state.run_id}:{key}"),
                content=content,
                trust=trust,
                source=ContextSource(
                    origin=trust,
                    reference=f"run:{state.run_id}:{key}",
                    detail=detail,
                ),
                sensitivity=DataSensitivity.INTERNAL,
                created_at=now,
            )

        if state.objective:
            items.append(
                item(
                    "objective",
                    state.objective,
                    TrustClass.AUTHORIZED_HUMAN,
                    "operator-supplied run objective",
                )
            )
        if state.plan:
            items.append(
                item(
                    "plan",
                    state.plan,
                    TrustClass.RUNTIME_POLICY,
                    "runtime-generated control plan",
                )
            )
        if state.last_observation is not None:
            items.append(
                item(
                    "observation",
                    state.last_observation,
                    TrustClass.DETERMINISTIC_OBSERVATION,
                    "latest journaled tool observation",
                )
            )
        if state.last_verification is not None:
            if state.last_verification_passed is None:
                outcome = "unknown"
            else:
                outcome = "passed" if state.last_verification_passed else "failed"
            items.append(
                item(
                    "verification",
                    state.last_verification,
                    TrustClass.DETERMINISTIC_OBSERVATION,
                    f"latest verifier outcome ({outcome})",
                )
            )

        return ModelContext(run_id=state.run_id, items=tuple(items), assembled_at=now)
