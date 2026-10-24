"""Live model adapter for Ollama's native chat API.

``OllamaModel`` is the first production ``ModelPort`` implementation backed by
a real provider. The provider owns nothing beyond text generation:

- the adapter receives only ``ModelContext`` (never ``RunState``) and renders
  the versioned ``PromptTemplate`` the runtime's context contract references —
  trust labels assembled by the context builder travel into the prompt
  verbatim, and untrusted fixture content stays ``UNTRUSTED_CONTENT``;
- tool authority stays code-owned: the adapter presents a caller-supplied
  ``ModelToolSpec`` catalog to the provider, but every proposal is
  re-authorized against runtime-owned ``ToolMetadata`` after parsing
  (AGENTS.md rule 4);
- provider responses are validated by strict schema before becoming an
  ``ActionProposal`` — malformed output raises ``ModelTurnError`` with
  ``MODEL_INVALID_RESPONSE`` (transient: the runtime reprompts with bounded
  backoff before it can become a durable stop reason);
- provider failures are normalized into the runtime's ``ModelFailureClass``
  vocabulary; no Ollama/HTTP semantics leak past this module;
- credentials (an optional bearer token) live only in the request headers and
  never enter model context, durable events, telemetry, or ``repr``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import Final, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from loopforge.adapters.observation_format import (
    checkpoint_stub,
    delimited,
    failure_fingerprint,
    is_verification_failure,
)
from loopforge.adapters.ollama_usage import ProviderRequestTracker, known_usage
from loopforge.domain.actions import ActionProposal
from loopforge.domain.admission import ContextAdmission, InputMeasurementBasis
from loopforge.domain.context import ContextItem, ContextSource, ModelContext
from loopforge.domain.prompts import PromptTemplate, RenderedPrompt, render_prompt
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.security import TrustClass
from loopforge.domain.types import ActionId, ContextItemId, RunId, UsageDelta
from loopforge.ports.model import (
    ModelFailureClass,
    ModelToolSpec,
    ModelTurn,
    ModelTurnError,
)

_CHAT_PATH: Final = "/api/chat"
# Read-only navigation whose result the model planned for; no fresh reasoning needed.
_FOLLOW_THROUGH_TOOLS: Final = frozenset(
    {"read_file", "search_files", "repository_map", "find_symbol", "workspace_status"}
)
_SUMMARY_BUDGET: Final = 200
# Projected growth of one more cycle: a tool result (12K-character read) plus a typical
# reasoning output (lab p90 about 1.1K tokens), in tokens.
_CHECKPOINT_RESERVE: Final = 6000
_KEEP_EXCHANGES: Final = 2
_VERIFICATION_SUFFIXES: Final = (":verifier-failure", ":confirmed-fact", ":verification")


class _FunctionCall(BaseModel):
    # Envelope tolerance: providers may add fields (Ollama adds ``index``);
    # strictness applies to the semantic contract validated below. Models
    # routinely omit ``arguments`` (or emit null) for zero-parameter tools;
    # both normalize to an empty argument mapping.
    model_config = ConfigDict(extra="ignore", strict=True)

    name: str
    arguments: dict[StrictStr, StrictStr] | None = None


class _ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    function: _FunctionCall


class _ResponseMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    role: str
    content: str = ""
    tool_calls: list[_ToolCall] | None = None
    thinking: str = Field(default="", max_length=100_000)


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    message: _ResponseMessage
    done: bool
    prompt_eval_count: int = Field(ge=0)
    eval_count: int = Field(ge=0)


class _ProviderError(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    error: str = Field(max_length=8192)


class OllamaModel:
    """``ModelPort`` implementation backed by an Ollama server's ``/api/chat``.

    The optional ``api_key`` is sent as a bearer token and is otherwise
    isolated: it is never rendered into prompts, never persisted, and never
    appears in this object's ``repr``. ``transport`` is the hermetic test
    seam (for example ``httpx.MockTransport``); production wiring leaves it
    ``None``. One instance is single-threaded by design, matching the
    runtime's synchronous control loop.
    """

    def __init__(  # noqa: PLR0912, PLR0913, PLR0915 - explicit provider wiring and validation
        self,
        *,
        model: str,
        tools: tuple[ModelToolSpec, ...],
        template: PromptTemplate,
        base_url: str = "http://localhost:11434",
        api_key: str | None = None,
        timeout_seconds: float = 300.0,
        capabilities: ModelCapabilities | None = None,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        thinking: bool | None = None,
        max_history_chars: int | None = None,
        history_overflow: Literal["trim", "fail"] = "trim",
        context_window_tokens: int | None = None,
        transport: httpx.BaseTransport | None = None,
        sampling_options: dict[str, float | int] | None = None,
        preserve_thinking: bool = False,
        keep_alive: str = "5m",
        request_token_counter: Callable[[dict[str, object]], int] | None = None,
        byte_ratio_admission: bool = False,
        thinking_policy: Literal["always", "on_evidence"] = "always",
        history_replay: Literal["bounded", "exact"] = "bounded",
        conversation_mode: Literal["history", "state"] = "history",
        observation_format: Literal["plain", "delimited"] = "plain",
        working_budget_tokens: int | None = None,
        checkpoint_summarizer: Callable[[list[dict[str, object]], str | None], str] | None = None,
        tool_filter: Callable[[ModelContext], frozenset[str]] | None = None,
        tool_spec_filter: Callable[[ModelContext, ModelToolSpec], ModelToolSpec] | None = None,
    ) -> None:
        if conversation_mode not in {"history", "state"}:
            msg = "conversation_mode must be history or state"
            raise ValueError(msg)
        if conversation_mode == "state" and preserve_thinking:
            msg = "state mode cannot retain model thinking"
            raise ValueError(msg)
        self._conversation_mode = conversation_mode
        self._protocol_feedback: dict[RunId, str] = {}
        if not model.strip():
            msg = "ollama model name cannot be empty"
            raise ValueError(msg)
        if not tools:
            msg_2 = "ollama model requires a non-empty tool catalog"
            raise ValueError(msg_2)
        names = [spec.name for spec in tools]
        if len(set(names)) != len(names):
            msg_3 = "ollama tool catalog names must be unique"
            raise ValueError(msg_3)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            msg_4 = "ollama timeout must be positive and finite"
            raise ValueError(msg_4)
        if not math.isfinite(temperature) or temperature < 0:
            msg_5 = "ollama temperature must be finite and non-negative"
            raise ValueError(msg_5)
        _validate_generation(max_output_tokens, thinking, max_history_chars)
        if history_overflow not in {"trim", "fail"}:
            msg = "history_overflow must be trim or fail"
            raise ValueError(msg)
        if context_window_tokens is not None and (
            type(context_window_tokens) is not int or context_window_tokens < 1024
        ):
            msg = "context_window_tokens must be an integer of at least 1024"
            raise ValueError(msg)
        try:
            parsed_url = httpx.URL(base_url)
        except httpx.InvalidURL as exc:
            msg_6 = f"ollama base URL is invalid: {base_url!r}"
            raise ValueError(msg_6) from exc
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
            msg_7 = "ollama base URL must be an absolute http(s) URL"
            raise ValueError(msg_7)
        if parsed_url.username or parsed_url.password:
            # Credentials in URLs are an un-audited second channel; the api_key
            # parameter is the only sanctioned credential path.
            msg_8 = "ollama base URL must not embed credentials; use api_key"
            raise ValueError(msg_8)
        # Capability identity is request identity: the payload sends
        # capabilities.model, so a wiring typo that diverges the two must
        # fail at construction, not silently query a different model.
        if capabilities is not None and (
            capabilities.provider != "ollama" or capabilities.model != model
        ):
            msg_9 = (
                "ollama capabilities identity must match the wired provider "
                f"and model (expected ollama/{model})"
            )
            raise ValueError(msg_9)
        self._capabilities = capabilities or ModelCapabilities(
            provider="ollama",
            model=model,
            supports_tool_calls=True,
            # Conservative default: the operator-facing capabilities parameter
            # should carry the deployed model's true window.
            context_window_tokens=131_072,
        )
        self._tools = tools
        self._tool_filter = tool_filter
        self._tool_spec_filter = tool_spec_filter
        self._template = template
        self.request_tracker = ProviderRequestTracker()
        self.request_recorder: Callable[[dict[str, object]], None] | None = None
        self.request_timeout_seconds = timeout_seconds
        self._turn_count = 0
        allowed_options = {"top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty"}
        if sampling_options is not None and (
            set(sampling_options) - allowed_options
            or any(
                type(v) not in {int, float} or not math.isfinite(v) or v < 0
                for v in sampling_options.values()
            )
        ):
            msg = "invalid code-owned sampling options"
            raise ValueError(msg)
        self._sampling_options = dict(sampling_options or {})
        self._preserve_thinking = preserve_thinking
        self._keep_alive = keep_alive
        self._request_token_counter = request_token_counter
        if type(byte_ratio_admission) is not bool or (
            byte_ratio_admission and request_token_counter is not None
        ):
            msg = "choose one code-owned request measurement"
            raise ValueError(msg)
        self._byte_ratio_admission = byte_ratio_admission
        if thinking_policy not in {"always", "on_evidence"} or (
            thinking_policy == "on_evidence" and conversation_mode != "history"
        ):
            msg = "evidence-driven thinking requires an append-only conversation"
            raise ValueError(msg)
        self._thinking_policy = thinking_policy
        # "exact" replays each assistant turn as generated (thinking and content
        # verbatim, no omission notices), so every request extends the previous
        # prompt plus generated tokens and prefix caches stay valid. Hybrid models
        # cannot trim recurrent cache state back to a rewritten history point.
        if history_replay not in {"bounded", "exact"} or (
            history_replay == "exact" and (conversation_mode != "history" or not preserve_thinking)
        ):
            msg = "exact history replay requires an append-only conversation preserving thinking"
            raise ValueError(msg)
        self._history_replay = history_replay
        if observation_format not in {"plain", "delimited"}:
            msg = "observation_format must be plain or delimited"
            raise ValueError(msg)
        self._observation_format = observation_format
        # Per run: fingerprint of the last verification failure presented in full.
        self._presented_failure: dict[RunId, str] = {}
        # Checkpoint compaction (code-owned usage budget below the allocated window).
        if working_budget_tokens is not None and (
            type(working_budget_tokens) is not int
            or working_budget_tokens < 8192
            or conversation_mode != "history"
            or observation_format != "delimited"
            or context_window_tokens is None
            or working_budget_tokens + _CHECKPOINT_RESERVE
            > context_window_tokens - (max_output_tokens or 2048) - 1024
        ):
            msg = (
                "working_budget_tokens must be an integer of at least 8192 in history mode "
                "with delimited observations, and its reserve inside the admitted window"
            )
            raise ValueError(msg)
        if checkpoint_summarizer is not None and working_budget_tokens is None:
            msg = "a checkpoint summarizer requires a working budget"
            raise ValueError(msg)
        self._working_budget = working_budget_tokens
        self._checkpoint_summarizer = checkpoint_summarizer
        self._checkpoint_due: set[RunId] = set()
        self._handoffs: dict[RunId, str] = {}
        # Per run: estimated prompt tokens right after the last checkpoint compaction.
        self._compacted_to: dict[RunId, int] = {}
        # Lab/operator metrics, one record per checkpoint compaction.
        self.compaction_log: list[dict[str, object]] = []
        self._ratio_bound_violated: set[RunId] = set()
        # Per run: (serialized bytes, provider-reported prompt tokens) of the last request.
        self._calibration: dict[RunId, tuple[int, int]] = {}
        self._history_compactions: dict[RunId, int] = {}
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._thinking = thinking
        self._max_history_chars = max_history_chars
        self._history_overflow = history_overflow
        self._context_window_tokens = context_window_tokens
        if context_window_tokens is not None and (
            context_window_tokens > self._capabilities.context_window_tokens
        ):
            msg = "requested context exceeds model capability window"
            raise ValueError(msg)
        # Per-run conversation state. Tool results must reach the provider as
        # structured tool messages — a flattened text observation makes models
        # re-issue the same read instead of acting on the result. State is
        # keyed by run id, rebuilt from context deltas each turn, and never
        # crosses runs; after a process crash it simply starts over from the
        # current context (safe: the runtime re-drives the loop).
        self._conversations: dict[RunId, list[dict[str, object]]] = {}
        self._seen_items: dict[RunId, set[tuple[str, str]]] = {}
        self._pending_calls: dict[RunId, str] = {}
        self._thinking_exhausted: set[RunId] = set()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def close(self) -> None:
        self._protocol_feedback.clear()
        self._conversations.clear()
        self._seen_items.clear()
        self._pending_calls.clear()
        self._thinking_exhausted.clear()
        self._client.close()

    def _selected_tools(self, context: ModelContext) -> tuple[ModelToolSpec, ...]:
        selected_tools = self._tools
        if self._tool_filter is not None:
            names = self._tool_filter(context)
            if not names or not names <= {tool.name for tool in self._tools}:
                raise ModelTurnError(
                    ModelFailureClass.PERMANENT,
                    "MODEL_TOOL_FILTER_INVALID",
                    "Code-owned tool filtering must select a nonempty registered subset",
                )
            selected_tools = tuple(tool for tool in self._tools if tool.name in names)
        if self._tool_spec_filter is not None:
            filtered = tuple(self._tool_spec_filter(context, spec) for spec in selected_tools)
            if tuple(spec.name for spec in filtered) != tuple(s.name for s in selected_tools):
                raise ModelTurnError(
                    ModelFailureClass.PERMANENT,
                    "MODEL_TOOL_FILTER_INVALID",
                    "Code-owned schema filtering must preserve registered tool identities",
                )
            selected_tools = filtered
        return selected_tools

    def propose_action(self, context: ModelContext) -> ModelTurn:
        selected_tools = self._selected_tools(context)
        rendered = self._render(context)
        # Decided before the conversation consumes the pending call: reason on the
        # first turn and after actions that produce new evidence (edits, checks,
        # feedback); follow-through reads use the plan already in the conversation.
        previous_tool = self._pending_calls.get(context.run_id)
        think_now = (
            context.run_id not in self._conversations
            or previous_tool is None
            or previous_tool not in _FOLLOW_THROUGH_TOOLS
        )
        messages = self._conversation(context, rendered)
        self._turn_count += 1
        payload: dict[str, object] = {
            "model": self._capabilities.model,
            "stream": False,
            "messages": list(messages),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
                for spec in selected_tools
            ],
            "options": {"temperature": self._temperature},
        }
        if self._max_output_tokens is not None:
            payload["options"] = {
                "temperature": self._temperature,
                "num_predict": self._max_output_tokens,
            }
        if self._thinking is not None:
            exhausted = context.run_id in self._thinking_exhausted
            if exhausted and self._thinking_policy == "on_evidence":
                # Evidence-driven runs skip reasoning for one turn to force an action,
                # then reason again on the next new evidence instead of never again.
                self._thinking_exhausted.discard(context.run_id)
            payload["think"] = (
                self._thinking
                and not exhausted
                and (self._thinking_policy == "always" or think_now)
            )
        if self._context_window_tokens is not None:
            options = {"temperature": self._temperature, "num_ctx": self._context_window_tokens}
            options["num_predict"] = self._max_output_tokens or 2048
            payload["options"] = options
        payload["keep_alive"] = self._keep_alive
        options = payload["options"]
        assert isinstance(options, dict)
        cast("dict[str, object]", options).update(self._sampling_options)
        effective_output_limit = self._max_output_tokens or (
            2048 if self._context_window_tokens is not None else None
        )
        self._checkpoint_compact(payload, messages, context)
        admission = self._admit_request(payload, messages, context)
        try:
            response = self._post(payload)
        except ModelTurnError as error:
            if self._conversation_mode == "state":
                self._protocol_feedback[context.run_id] = (
                    "Previous provider response failed validation; no action executed. "
                    "Return exactly ONE schema-valid native tool call. Authority is unchanged."
                )
            error.context_admission = admission
            if error.reason_code == "MODEL_TOOL_PARSE":
                messages.append(
                    {
                        "role": self._correction_role(),
                        "content": (
                            "Protocol correction: the provider rejected the previous tool-call "
                            "syntax; no action executed. Return exactly ONE well-formed native "
                            "tool call with schema-valid arguments. Do not emit hand-written "
                            "XML tags, prose or multiple tool calls. Tool permissions, budgets "
                            "and checks are unchanged."
                        ),
                    }
                )
            raise
        usage = UsageDelta(
            cost_usd=self._cost(response),
            input_tokens=response.prompt_eval_count,
            output_tokens=response.eval_count,
        )
        self._record_measurement(context, admission, response.prompt_eval_count)
        try:
            action = self._parse_action(context, response)
        except ModelTurnError as error:
            if self._conversation_mode == "state":
                self._protocol_feedback[context.run_id] = (
                    "Previous response did not contain exactly one valid native action; "
                    "no action executed. Return ONE schema-valid tool call, then wait."
                )
            summary = error.summary
            if (
                self._thinking is True
                and effective_output_limit is not None
                and response.eval_count >= effective_output_limit
                and not response.message.tool_calls
            ):
                self._thinking_exhausted.add(context.run_id)
                self.request_tracker.note("thinking_disabled_after_output_exhaustion")
                summary += (
                    "; output allowance exhausted without an action; the next call disables "
                    "thinking"
                    if self._thinking_policy == "on_evidence"
                    else "; output allowance exhausted without an action; next calls disable "
                    "thinking"
                )
            raise ModelTurnError(
                error.failure_class,
                error.reason_code,
                summary,
                usage=usage,
                context_admission=admission,
            ) from error
        self._protocol_feedback.pop(context.run_id, None)
        return ModelTurn(action=action, usage=usage, context_admission=admission)

    def _admit_request(
        self, payload: dict[str, object], messages: list[dict[str, object]], context: ModelContext
    ) -> ContextAdmission | None:
        """Measure the final request and compact complete turns inside its fixed envelope."""
        if self._context_window_tokens is None:
            return None
        output_reserve = self._max_output_tokens or 2048
        input_budget = self._context_window_tokens - output_reserve - 1024
        compacting = False
        while True:
            serialized_bytes = len(
                json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode()
            )
            count, basis = self._measure(
                payload, serialized_bytes, context, calibrated=not compacting
            )
            if type(count) is not int or count < 0:
                msg = "request token counter returned invalid occupancy"
                raise ValueError(msg)
            admission = ContextAdmission(
                measurement_basis=basis,
                input_token_bound=count,
                serialized_request_bytes=serialized_bytes,
                context_window_tokens=self._context_window_tokens,
                output_reserve_tokens=output_reserve,
                framing_reserve_tokens=1024,
                compacted_turns=self._history_compactions.get(context.run_id, 0),
            )
            # Each compaction rewrites the prompt prefix (a full re-prefill), so once
            # it is needed it frees headroom to 75% of the input budget at once.
            if admission.admitted and (not compacting or count <= input_budget * 3 // 4):
                self.request_tracker.record_admission(admission)
                return admission
            if admission.admitted:
                # Short of the headroom target: drop another turn, or accept if none remain.
                if not self._compact_history(messages, context):
                    self.request_tracker.record_admission(admission)
                    return admission
                payload["messages"] = list(messages)
                continue
            compacting = True
            if (
                self._conversation_mode == "state"
                or self._history_overflow == "fail"
                or not self._compact_history(messages, context)
            ):
                self.request_tracker.record_admission(admission)
                raise ModelTurnError(
                    ModelFailureClass.PERMANENT,
                    "MODEL_CONTEXT_LIMIT",
                    "Required prompt, tools and latest complete turn exceed context envelope",
                    context_admission=admission,
                )
            payload["messages"] = list(messages)

    def _record_measurement(
        self, context: ModelContext, admission: ContextAdmission | None, prompt_tokens: int
    ) -> None:
        """Calibrate the next bound from the provider's count, or fail closed if exceeded."""
        if admission is None or admission.measurement_basis not in {
            InputMeasurementBasis.UTF8_BYTES_PER_TOKEN_FLOOR_2,
            InputMeasurementBasis.PROVIDER_COUNT_PLUS_DELTA,
        }:
            return
        if prompt_tokens > admission.input_token_bound:
            # The ratio assumption failed for this run: fail closed to the strict bound.
            self._ratio_bound_violated.add(context.run_id)
            self._calibration.pop(context.run_id, None)
            self.request_tracker.note("byte_ratio_bound_exceeded_strict_bound_restored")
            return
        self._calibration[context.run_id] = (admission.serialized_request_bytes, prompt_tokens)

    def _measure(
        self,
        payload: dict[str, object],
        serialized_bytes: int,
        context: ModelContext,
        *,
        calibrated: bool = True,
    ) -> tuple[int, InputMeasurementBasis]:
        if self._request_token_counter is not None:
            return self._request_token_counter(payload), InputMeasurementBasis.TOKEN_COUNTER
        if not self._byte_ratio_admission or context.run_id in self._ratio_bound_violated:
            return serialized_bytes, InputMeasurementBasis.UTF8_BYTE_UPPER_BOUND
        # The delta assumes "previous request plus appended turns"; a compacted request
        # removed turns and inserted a notice, so it uses the full ratio until the next
        # provider count re-calibrates.
        calibration = self._calibration.get(context.run_id) if calibrated else None
        if calibration is not None and serialized_bytes >= calibration[0]:
            delta = -(-(serialized_bytes - calibration[0]) // 2)
            return calibration[1] + delta, InputMeasurementBasis.PROVIDER_COUNT_PLUS_DELTA
        # First request, or the history shrank (compaction): full conservative ratio.
        return -(-serialized_bytes // 2), InputMeasurementBasis.UTF8_BYTES_PER_TOKEN_FLOOR_2

    def _estimate(self, payload: dict[str, object], context: ModelContext) -> int:
        """Planning estimate of prompt tokens from the run's last provider count."""
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        calibration = self._calibration.get(context.run_id)
        if calibration is None or calibration[0] <= 0:
            return -(-size // 3)
        return math.ceil(size * calibration[1] / calibration[0])

    def _checkpoint_compact(  # noqa: PLR0912 - ordered checkpoint, tier-1 and tier-2 gates
        self, payload: dict[str, object], messages: list[dict[str, object]], context: ModelContext
    ) -> None:
        """Compact at a deterministic checkpoint so usage stays under the working budget.

        Checkpoints are verification results. At a checkpoint the next cycle is
        projected (current prompt plus one tool result and output); if it would exceed
        the budget, history is compacted to half the budget in one rewrite. Between
        checkpoints only an actual overrun compacts (a backstop, recorded as such).
        Tier 1 masks old tool output to restorable stubs and drops old thinking; tier 2
        (optional) replaces the masked middle with a validated model-inference handoff.
        """
        budget = self._working_budget
        if budget is None or self._conversation_mode != "history":
            return
        checkpoint = context.run_id in self._checkpoint_due
        self._checkpoint_due.discard(context.run_id)
        before = self._estimate(payload, context)
        overrun = before > budget
        if not overrun and not (checkpoint and before + _CHECKPOINT_RESERVE > budget):
            return
        previous = self._compacted_to.get(context.run_id)
        if previous is not None and before < previous + _CHECKPOINT_RESERVE:
            # Too little growth since the last rewrite for another one to free a cycle;
            # each rewrite is a full re-prefill, so repeating it would only thrash.
            return
        if not messages or messages[-1].get("role") != "tool":
            return
        target = budget // 2
        record: dict[str, object] = {
            "run_id": str(context.run_id),
            "trigger": "checkpoint" if checkpoint else "budget_overrun",
            "tokens_before": before,
            "tier": 1,
        }
        assistants = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
        if len(assistants) <= _KEEP_EXCHANGES:
            return
        keep_from = assistants[-_KEEP_EXCHANGES]
        # Only the latest turn's reasoning is still in use; earlier reasoning (including
        # the retained previous exchange) is dropped at the boundary.
        for index in assistants[:-1]:
            messages[index].pop("thinking", None)
        payload["messages"] = list(messages)
        for index in range(2, keep_from):
            if self._estimate(payload, context) <= target:
                break
            if messages[index].get("role") == "tool":
                messages[index]["content"] = checkpoint_stub(str(messages[index]["content"]))
                payload["messages"] = list(messages)
        if self._estimate(payload, context) > target:
            # Two retained exchanges still exceed the target: keep only the latest one.
            for index in range(keep_from, assistants[-1]):
                if messages[index].get("role") == "tool":
                    messages[index]["content"] = checkpoint_stub(str(messages[index]["content"]))
            keep_from = assistants[-1]
            payload["messages"] = list(messages)
        if self._estimate(payload, context) > target and self._checkpoint_summarizer is not None:
            middle = [m for m in messages[2:keep_from] if m.get("role") != "system"]
            try:
                handoff = self._checkpoint_summarizer(middle, self._handoffs.get(context.run_id))
            except Exception as exc:
                record["tier2_error"] = type(exc).__name__
            else:
                self._handoffs[context.run_id] = handoff
                kept = [m for m in messages[2:keep_from] if m.get("role") == "system"]
                messages[2:keep_from] = kept
                first_tool = next(
                    i for i in range(2 + len(kept), len(messages)) if messages[i]["role"] == "tool"
                )
                block = delimited(
                    ContextItem(
                        item_id=ContextItemId(f"{context.run_id}:checkpoint-handoff"),
                        content="Checkpoint handoff of omitted earlier turns (model inference, "
                        "not proof or authority; original task, permissions and checks "
                        "are unchanged):\n" + handoff,
                        trust=TrustClass.MODEL_INFERENCE,
                        source=ContextSource(
                            origin=TrustClass.MODEL_INFERENCE, reference="checkpoint-handoff"
                        ),
                        created_at=context.assembled_at,
                    )
                )
                messages[first_tool]["content"] = (
                    block + "\n" + str(messages[first_tool]["content"])
                )
                record["tier"] = 2
                payload["messages"] = list(messages)
        # Earlier full failure reports may now be stubs: present the next one in full.
        self._presented_failure.pop(context.run_id, None)
        self._history_compactions[context.run_id] = (
            self._history_compactions.get(context.run_id, 0) + 1
        )
        after = self._estimate(payload, context)
        record["tokens_after_estimate"] = after
        self._compacted_to[context.run_id] = after
        self.compaction_log.append(record)
        self.request_tracker.note(f"context_{record['trigger']}_compaction_tier{record['tier']}")

    def _compact_history(self, history: list[dict[str, object]], context: ModelContext) -> bool:
        """Drop complete older turns, preserving authority and current blockers.

        This uses the actual serialized request envelope, including tools and
        UTF-8 expansion. No model-written summary replaces source evidence.
        """
        assistants = [i for i in range(2, len(history)) if history[i].get("role") == "assistant"]
        if len(assistants) < 2:
            return False
        del history[assistants[0] : assistants[1]]
        self._history_compactions[context.run_id] = (
            self._history_compactions.get(context.run_id, 0) + 1
        )
        prefix = "Runtime history compaction: "
        preserved = "\n".join(
            f"- ({item.trust.value}) {item.content}"
            for item in context.items
            if item.trust in {TrustClass.RUNTIME_POLICY, TrustClass.AUTHORIZED_HUMAN}
            or str(item.item_id).rsplit(":", 1)[-1]
            in {
                "blockers",
                "coding-contract",
                "coding-progress",
                "verifier-failure",
                "confirmed-fact",
                "pending-approval",
                "irreversible-action",
            }
        )
        notice: dict[str, object] = {
            "role": "user",
            "content": prefix + "older complete action/result turns were omitted to fit "
            "the unchanged request envelope. Original objective and latest turn remain. "
            "Earlier source observations may be absent: reread needed source; never infer "
            "unseen evidence. Permissions, checks, approvals and budgets are unchanged. "
            "Current runtime facts retain their trust labels:\n" + preserved,
        }
        if (
            len(history) > 2
            and history[2].get("role") == "user"
            and str(history[2].get("content", "")).startswith(prefix)
        ):
            history[2] = notice
        else:
            history.insert(2, notice)
        return True

    def drop_conversation(self, run_id: RunId) -> None:
        """Discard conversation state for a finished run (memory hygiene)."""
        self._protocol_feedback.pop(run_id, None)
        self._conversations.pop(run_id, None)
        self._seen_items.pop(run_id, None)
        self._pending_calls.pop(run_id, None)
        self._history_compactions.pop(run_id, None)
        self._ratio_bound_violated.discard(run_id)
        self._calibration.pop(run_id, None)
        self._presented_failure.pop(run_id, None)
        self._checkpoint_due.discard(run_id)
        self._handoffs.pop(run_id, None)
        self._compacted_to.pop(run_id, None)

    def _conversation(
        self, context: ModelContext, rendered: RenderedPrompt
    ) -> list[dict[str, object]]:
        if self._conversation_mode == "state":
            # The request is rebuilt from the current projection, not prior chat.
            # Audit history remains in the event store; no model summary replaces it.
            messages: list[dict[str, object]] = [
                {"role": "system", "content": rendered.stable_prefix},
                {"role": "user", "content": rendered.dynamic_suffix or "No current state items."},
            ]
            correction = self._protocol_feedback.get(context.run_id)
            if correction is not None:
                messages.append({"role": "user", "content": correction})
            if (
                self._max_history_chars is not None
                and len(json.dumps(messages, ensure_ascii=False)) > self._max_history_chars
            ):
                raise ModelTurnError(
                    ModelFailureClass.PERMANENT,
                    "MODEL_CONTEXT_LIMIT",
                    "Current state packet exceeds its fixed envelope; narrow the node",
                )
            self.request_tracker.note("fresh_state_request_no_chat_replay")
            return messages
        history: list[dict[str, object]] | None = self._conversations.get(context.run_id)
        seen = self._seen_items.setdefault(context.run_id, set())
        new_items = [item for item in context.items if (item.item_id, item.content) not in seen]
        if history is None:
            history = [
                {"role": "system", "content": rendered.stable_prefix},
                {
                    "role": "user",
                    "content": rendered.dynamic_suffix or "No run context items this turn.",
                },
            ]
            self._conversations[context.run_id] = history
        else:
            self._append_turn_delta(context, history, new_items)
            if any(str(item.item_id).endswith(_VERIFICATION_SUFFIXES) for item in new_items):
                # A verification result arrived: a deterministic checkpoint.
                self._checkpoint_due.add(context.run_id)
        seen.update((item.item_id, item.content) for item in context.items)
        if self._max_history_chars is not None:
            while len(json.dumps(history, ensure_ascii=False)) > self._max_history_chars:
                if self._history_overflow == "fail":
                    raise ModelTurnError(
                        ModelFailureClass.PERMANENT,
                        "MODEL_CONTEXT_LIMIT",
                        "Append-only conversation exceeds history limit; start a smaller unit",
                    )
                if not self._compact_history(history, context):
                    raise ModelTurnError(
                        ModelFailureClass.PERMANENT,
                        "MODEL_CONTEXT_LIMIT",
                        "System prompt, current facts and latest turn exceed history limit",
                    )
        return history

    def _append_turn_delta(
        self,
        context: ModelContext,
        history: list[dict[str, object]],
        new_items: list[ContextItem],
    ) -> None:
        pending = self._pending_calls.pop(context.run_id, None)
        if pending is not None:
            # The runtime executed (or rejected) the previous proposal; its
            # observation must answer the pending tool call. Observation-trust
            # items are the runtime's tool-result vocabulary (demoted to
            # UNTRUSTED_CONTENT by workloads that handle repository output).
            observations = [
                item
                for item in new_items
                if item.trust
                in {TrustClass.DETERMINISTIC_OBSERVATION, TrustClass.UNTRUSTED_CONTENT}
            ]
            # Trust labels travel with the content: the model must be able to
            # distinguish runtime-owned observations from untrusted repository
            # output at the exact point tool results re-enter the conversation.
            result = (
                "\n".join(self._present(context.run_id, item) for item in observations)
                if observations
                else self._no_observation(pending)
            )
            new_items = [item for item in new_items if item not in observations]
            if self._history_replay == "exact" and new_items:
                # A separate user message would become the renderer's latest query
                # and re-render every earlier assistant turn; keep runtime updates
                # inside the tool response, with their trust labels.
                result += "\nRuntime update:\n" + "\n".join(
                    self._present(context.run_id, item) for item in new_items
                )
                new_items = []
            history.append({"role": "tool", "tool_name": pending, "content": result})
        if new_items:
            lines = "\n".join(self._present(context.run_id, item) for item in new_items)
            history.append({"role": "user", "content": f"Runtime update:\n{lines}"})

    def _no_observation(self, tool_name: str) -> str:
        """Code-owned result for a proposal that produced no observation."""
        names = [spec.name for spec in self._tools]
        if tool_name not in names:
            return (
                f"Rejected: {tool_name!r} is not an available tool; no action executed. "
                f"Available tools: {', '.join(names)}."
            )
        return "No new observation was recorded for the proposed action."

    def _present(self, run_id: RunId, item: ContextItem) -> str:
        """Code-owned item presentation inside the conversation (see observation_format)."""
        if self._observation_format == "plain":
            return f"- ({item.trust.value}) {item.content}"
        if is_verification_failure(item):
            fingerprint = failure_fingerprint(item.content)
            if fingerprint and self._presented_failure.get(run_id) == fingerprint:
                return (
                    f"- ({item.trust.value}) verification failed again with the same checks, "
                    f"failing tests and assertions as the last full report above: {fingerprint}"
                )
            self._presented_failure[run_id] = fingerprint
        return delimited(item)

    def _correction_role(self) -> str:
        """Runtime-owned corrections; in exact replay a mid-conversation system message.

        A user message would become the renderer's latest query and re-render every
        earlier assistant turn, discarding the prefix cache on hybrid models.
        """
        return "system" if self._history_replay == "exact" else "user"

    def _render(self, context: ModelContext) -> RenderedPrompt:
        reference = self._template.reference()
        if context.prompt_template != reference:
            msg = (
                f"context references prompt template {context.prompt_template}, "
                f"adapter is bound to {reference}"
            )
            raise ModelTurnError(
                ModelFailureClass.PERMANENT,
                "MODEL_PROMPT_TEMPLATE_MISMATCH",
                _bounded(msg),
            )
        return render_prompt(self._template, context, role=context.role)

    def _post(self, payload: dict[str, object]) -> ChatResponse:
        if self.request_recorder is not None:
            self.request_recorder(payload)
        try:
            self.request_tracker.begin(self.request_timeout_seconds, "controller")
            try:
                raw = self._client.post(
                    _CHAT_PATH, json=payload, timeout=self.request_timeout_seconds
                )
            finally:
                self.request_tracker.end()
        except httpx.TimeoutException as exc:
            msg = f"provider request timed out: {type(exc).__name__}"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_TIMEOUT", _bounded(msg)
            ) from exc
        except httpx.DecodingError as exc:
            # A 200 with a corrupt content-encoding is malformed provider
            # output. httpx reads bodies eagerly and DecodingError is a
            # RequestError, not a TransportError — without its own clause it
            # escapes the failure taxonomy entirely.
            msg = "provider response body could not be decoded"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_INVALID_RESPONSE", msg
            ) from exc
        except httpx.RequestError as exc:
            # TransportError and every other request-scoped failure
            # (TooManyRedirects, protocol errors) is a provider-side condition.
            msg = f"provider transport failure: {type(exc).__name__}"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_UNAVAILABLE", _bounded(msg)
            ) from exc
        if raw.status_code != 200:
            if raw.status_code == 500 and len(raw.content) <= 8192:
                try:
                    error = _ProviderError.model_validate_json(raw.content).error.lower()
                except ValueError:
                    error = ""
                if ("pars" in error and "tool call" in error) or re.fullmatch(
                    r"xml syntax error on line [1-9][0-9]*: "
                    r"element <function> closed by </parameter>",
                    error,
                ):
                    # Inspect only for a fixed error category. Never echo the
                    # provider body, which may include prompt or secret text.
                    raise ModelTurnError(
                        ModelFailureClass.TRANSIENT,
                        "MODEL_TOOL_PARSE",
                        "Provider rejected generated tool-call syntax (HTTP 500); "
                        "no action executed; usage unavailable",
                    )
            self._raise_for_status(raw.status_code)
        try:
            data = raw.json()
        except json.JSONDecodeError as exc:
            msg = "provider response was not valid JSON"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_INVALID_RESPONSE", msg
            ) from exc
        try:
            self.request_tracker.record(data)
            parsed = ChatResponse.model_validate(data)
        except ValidationError as exc:
            msg = f"provider response violated the chat schema: {exc.error_count()} errors"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT,
                "MODEL_INVALID_RESPONSE",
                _bounded(msg),
                usage=known_usage(data),
            ) from exc
        if not parsed.done:
            msg_2 = "provider response was not a completed generation"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT,
                "MODEL_INVALID_RESPONSE",
                msg_2,
                usage=known_usage(data),
            )
        return parsed

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        # Provider error bodies never enter the runtime: they can embed prompt
        # fragments or provider internals, so failures carry only the status.
        if status_code in (401, 403):
            msg = f"provider rejected credentials (HTTP {status_code})"
            raise ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_AUTHENTICATION", msg)
        if status_code == 404:
            msg = "provider does not serve the requested model (HTTP 404)"
            raise ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_NOT_FOUND", msg)
        if status_code == 429 or status_code >= 500:
            msg = f"provider is unavailable (HTTP {status_code})"
            raise ModelTurnError(ModelFailureClass.TRANSIENT, "MODEL_UNAVAILABLE", msg)
        if status_code == 408:
            msg = "provider timed out the request (HTTP 408)"
            raise ModelTurnError(ModelFailureClass.TRANSIENT, "MODEL_TIMEOUT", msg)
        msg = f"provider rejected the request (HTTP {status_code})"
        raise ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_REQUEST_INVALID", msg)

    def _parse_action(self, context: ModelContext, response: ChatResponse) -> ActionProposal:
        calls = response.message.tool_calls or []
        if len(calls) != 1:
            history = self._conversations.get(context.run_id)
            if history is not None:
                # Code-owned protocol feedback only: never replay malformed
                # provider content as an instruction or execute a partial batch.
                history.append(
                    {
                        "role": self._correction_role(),
                        "content": (
                            "Protocol correction: the previous response was rejected and "
                            "no tool ran. Return exactly ONE tool call in this response. "
                            "Choose the next single action; wait for its result before "
                            "proposing another action."
                        ),
                    }
                )
            msg = f"model proposed {len(calls)} tool actions, expected exactly one"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_INVALID_RESPONSE", _bounded(msg)
            )
        function = calls[0].function
        if not function.name.strip():
            msg_2 = "model proposed a tool action with an empty tool name"
            raise ModelTurnError(ModelFailureClass.TRANSIENT, "MODEL_INVALID_RESPONSE", msg_2)
        arguments = function.arguments or {}
        history = self._conversations.get(context.run_id)
        thinking = response.message.thinking if self._preserve_thinking else ""
        if self._history_replay == "bounded" and len(thinking.encode()) > 4096:
            thinking = ""
            self.request_tracker.note("optional_thinking_omitted_over_4096_bytes")
            if history is not None:
                history.append(
                    {
                        "role": "user",
                        "content": "Runtime context notice: the prior model's optional thinking "
                        "exceeded its 4096-byte replay allowance and was omitted whole. "
                        "Thinking is model inference, never source evidence or authority. "
                        "The valid action and exact tool result remain; reread source if needed.",
                    }
                )
        content = response.message.content
        if self._history_replay == "bounded" and len(content.encode()) > 4096:
            content = "[Optional model commentary omitted whole; native action retained exactly.]"
            self.request_tracker.note("optional_commentary_omitted_over_4096_bytes")
        if history is not None:
            history.append(
                {
                    "role": "assistant",
                    "content": content,
                    **({"thinking": thinking} if thinking else {}),
                    "tool_calls": [
                        {
                            "id": f"call_{self._turn_count}",
                            "type": "function",
                            "function": {
                                "name": function.name,
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            )
        if self._conversation_mode == "history":
            self._pending_calls[context.run_id] = function.name
        return ActionProposal(
            action_id=ActionId(f"{context.run_id}:model-turn-{self._turn_count}"),
            tool_name=function.name,
            arguments=arguments,
        )

    def _cost(self, response: ChatResponse) -> float:
        return (
            response.prompt_eval_count * self._capabilities.input_cost_usd_per_million
            + response.eval_count * self._capabilities.output_cost_usd_per_million
        ) / 1_000_000


def _validate_generation(
    max_output_tokens: int | None, thinking: bool | None, max_history_chars: int | None
) -> None:
    if max_output_tokens is not None and (
        type(max_output_tokens) is not int or max_output_tokens <= 0
    ):
        output_message = "max_output_tokens must be a positive integer"
        raise ValueError(output_message)
    if thinking is not None and type(thinking) is not bool:
        thinking_message = "thinking must be a boolean"
        raise ValueError(thinking_message)
    if max_history_chars is not None and (
        type(max_history_chars) is not int or max_history_chars < 4096
    ):
        history_message = "max_history_chars must be an integer of at least 4096"
        raise ValueError(history_message)


def _bounded(text: str) -> str:
    sanitized = "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in text)
    if len(sanitized) <= _SUMMARY_BUDGET:
        return sanitized
    return sanitized[: _SUMMARY_BUDGET - 3] + "..."
