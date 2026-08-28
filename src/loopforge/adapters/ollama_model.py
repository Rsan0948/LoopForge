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
  ``MODEL_INVALID_RESPONSE`` and never touches the event log;
- provider failures are normalized into the runtime's ``ModelFailureClass``
  vocabulary; no Ollama/HTTP semantics leak past this module;
- credentials (an optional bearer token) live only in the request headers and
  never enter model context, durable events, telemetry, or ``repr``.
"""

from __future__ import annotations

import json
import math
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ContextItem, ModelContext
from loopforge.domain.prompts import PromptTemplate, RenderedPrompt, render_prompt
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.security import TrustClass
from loopforge.domain.types import ActionId, RunId, UsageDelta
from loopforge.ports.model import (
    ModelFailureClass,
    ModelToolSpec,
    ModelTurn,
    ModelTurnError,
)

_CHAT_PATH: Final = "/api/chat"
_SUMMARY_BUDGET: Final = 200


class _FunctionCall(BaseModel):
    # Envelope tolerance: providers may add fields (Ollama adds ``index``);
    # strictness applies to the semantic contract validated below. Models
    # routinely omit ``arguments`` (or emit null) for zero-parameter tools;
    # both normalize to an empty argument mapping.
    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: dict[StrictStr, StrictStr] | None = None


class _ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    function: _FunctionCall


class _ResponseMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str = ""
    tool_calls: list[_ToolCall] | None = None


class _ChatResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: _ResponseMessage
    done: bool
    prompt_eval_count: int = Field(default=0, ge=0)
    eval_count: int = Field(default=0, ge=0)


class OllamaModel:
    """``ModelPort`` implementation backed by an Ollama server's ``/api/chat``.

    The optional ``api_key`` is sent as a bearer token and is otherwise
    isolated: it is never rendered into prompts, never persisted, and never
    appears in this object's ``repr``. ``transport`` is the hermetic test
    seam (for example ``httpx.MockTransport``); production wiring leaves it
    ``None``. One instance is single-threaded by design, matching the
    runtime's synchronous control loop.
    """

    def __init__(  # noqa: PLR0913 - keyword-only wiring keeps every adapter dependency explicit
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
        transport: httpx.BaseTransport | None = None,
    ) -> None:
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
        self._template = template
        self._turn_count = 0
        self._temperature = temperature
        # Per-run conversation state. Tool results must reach the provider as
        # structured tool messages — a flattened text observation makes models
        # re-issue the same read instead of acting on the result. State is
        # keyed by run id, rebuilt from context deltas each turn, and never
        # crosses runs; after a process crash it simply starts over from the
        # current context (safe: the runtime re-drives the loop).
        self._conversations: dict[RunId, list[dict[str, object]]] = {}
        self._seen_items: dict[RunId, set[tuple[str, str]]] = {}
        self._pending_calls: dict[RunId, str] = {}
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
        self._conversations.clear()
        self._seen_items.clear()
        self._pending_calls.clear()
        self._client.close()

    def propose_action(self, context: ModelContext) -> ModelTurn:
        rendered = self._render(context)
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
                for spec in self._tools
            ],
            "options": {"temperature": self._temperature},
        }
        response = self._post(payload)
        action = self._parse_action(context, response)
        usage = UsageDelta(
            cost_usd=self._cost(response),
            input_tokens=response.prompt_eval_count,
            output_tokens=response.eval_count,
        )
        return ModelTurn(action=action, usage=usage)

    def drop_conversation(self, run_id: RunId) -> None:
        """Discard conversation state for a finished run (memory hygiene)."""
        self._conversations.pop(run_id, None)
        self._seen_items.pop(run_id, None)
        self._pending_calls.pop(run_id, None)

    def _conversation(
        self, context: ModelContext, rendered: RenderedPrompt
    ) -> list[dict[str, object]]:
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
        seen.update((item.item_id, item.content) for item in context.items)
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
                "\n".join(f"- ({item.trust.value}) {item.content}" for item in observations)
                if observations
                else "No new observation was recorded for the proposed action."
            )
            history.append({"role": "tool", "name": pending, "content": result})
            new_items = [item for item in new_items if item not in observations]
        if new_items:
            lines = "\n".join(f"- ({item.trust.value}) {item.content}" for item in new_items)
            history.append({"role": "user", "content": f"Runtime update:\n{lines}"})

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

    def _post(self, payload: dict[str, object]) -> _ChatResponse:
        try:
            raw = self._client.post(_CHAT_PATH, json=payload)
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
                ModelFailureClass.PERMANENT, "MODEL_INVALID_RESPONSE", msg
            ) from exc
        except httpx.RequestError as exc:
            # TransportError and every other request-scoped failure
            # (TooManyRedirects, protocol errors) is a provider-side condition.
            msg = f"provider transport failure: {type(exc).__name__}"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_UNAVAILABLE", _bounded(msg)
            ) from exc
        if raw.status_code != 200:
            self._raise_for_status(raw.status_code)
        try:
            data = raw.json()
        except json.JSONDecodeError as exc:
            msg = "provider response was not valid JSON"
            raise ModelTurnError(
                ModelFailureClass.PERMANENT, "MODEL_INVALID_RESPONSE", msg
            ) from exc
        try:
            parsed = _ChatResponse.model_validate(data)
        except ValidationError as exc:
            msg = f"provider response violated the chat schema: {exc.error_count()} errors"
            raise ModelTurnError(
                ModelFailureClass.PERMANENT, "MODEL_INVALID_RESPONSE", _bounded(msg)
            ) from exc
        if not parsed.done:
            msg_2 = "provider response was not a completed generation"
            raise ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_INVALID_RESPONSE", msg_2)
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

    def _parse_action(self, context: ModelContext, response: _ChatResponse) -> ActionProposal:
        calls = response.message.tool_calls or []
        if len(calls) != 1:
            msg = f"model proposed {len(calls)} tool actions, expected exactly one"
            raise ModelTurnError(
                ModelFailureClass.PERMANENT, "MODEL_INVALID_RESPONSE", _bounded(msg)
            )
        function = calls[0].function
        if not function.name.strip():
            msg_2 = "model proposed a tool action with an empty tool name"
            raise ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_INVALID_RESPONSE", msg_2)
        arguments = function.arguments or {}
        history = self._conversations.get(context.run_id)
        if history is not None:
            history.append(
                {
                    "role": "assistant",
                    "content": "",
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
        self._pending_calls[context.run_id] = function.name
        return ActionProposal(
            action_id=ActionId(f"{context.run_id}:model-turn-{self._turn_count}"),
            tool_name=function.name,
            arguments=arguments,
        )

    def _cost(self, response: _ChatResponse) -> float:
        return (
            response.prompt_eval_count * self._capabilities.input_cost_usd_per_million
            + response.eval_count * self._capabilities.output_cost_usd_per_million
        ) / 1_000_000


def _bounded(text: str) -> str:
    sanitized = "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in text)
    if len(sanitized) <= _SUMMARY_BUDGET:
        return sanitized
    return sanitized[: _SUMMARY_BUDGET - 3] + "..."
