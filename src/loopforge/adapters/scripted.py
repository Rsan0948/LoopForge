from __future__ import annotations

from collections import deque
from datetime import datetime

from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.state import RunState
from loopforge.domain.tooling import ToolMetadata
from loopforge.domain.types import UsageDelta
from loopforge.ports.model import ModelTurn
from loopforge.ports.tools import ToolExecutionRequest, ToolResult, UnknownToolError
from loopforge.ports.verifier import VerificationResult


class ScriptedModel:
    def __init__(
        self,
        actions: list[ActionProposal],
        *,
        cost_per_turn: float = 0.01,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        self._actions = deque(actions)
        self._cost_per_turn = cost_per_turn
        self._capabilities = capabilities or ModelCapabilities(
            provider="scripted",
            model="scripted-deterministic",
            supports_tool_calls=True,
            # The scripted model is deterministic and never reads context;
            # the declared window honestly matches the runtime's wired
            # context budget rather than implying unbounded capacity.
            context_window_tokens=4096,
        )

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def propose_action(self, context: ModelContext) -> ModelTurn:
        del context
        if not self._actions:
            msg = "scripted model exhausted"
            raise RuntimeError(msg)
        return ModelTurn(
            action=self._actions.popleft(),
            usage=UsageDelta(cost_usd=self._cost_per_turn, input_tokens=100, output_tokens=20),
        )


class ScriptedTools:
    def __init__(
        self,
        results: list[ToolResult],
        *,
        metadata: list[ToolMetadata],
    ) -> None:
        self._results = deque(results)
        self._metadata = {item.name: item for item in metadata}

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        try:
            return self._metadata[tool_name]
        except KeyError as exc:
            msg = f"unknown tool: {tool_name}"
            raise UnknownToolError(msg) from exc

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        # Enforce registration at execution as defense in depth.
        self.metadata_for(request.proposal.tool_name)
        if not self._results:
            msg = "scripted tool results exhausted"
            raise RuntimeError(msg)
        return self._results.popleft()


class ObservationContainsVerifier:
    def __init__(self, expected: str) -> None:
        self._expected = expected

    def verify(self, state: RunState) -> VerificationResult:
        observation = state.last_observation or ""
        passed = self._expected in observation
        return VerificationResult(passed=passed, summary=f"expected {self._expected!r}")


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
