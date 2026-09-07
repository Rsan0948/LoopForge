"""DeepSeek chat-completions adapter for the bounded LoopForge model port."""

from __future__ import annotations

import json
import math
from typing import Final, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loopforge.adapters.ollama_model import ChatResponse, OllamaModel
from loopforge.domain.context import ModelContext
from loopforge.domain.prompts import PromptTemplate
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.types import UsageDelta
from loopforge.ports.model import ModelFailureClass, ModelToolSpec, ModelTurn, ModelTurnError

_CHAT_PATH: Final = "/chat/completions"
_SUMMARY_BUDGET: Final = 200


class _FunctionCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: str = "{}"


class _ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    function: _FunctionCall


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_ToolCall] | None = None


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: _Message
    finish_reason: str | None = None


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    prompt_cache_hit_tokens: int = Field(default=0, ge=0)


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")

    choices: list[_Choice]
    usage: _Usage = Field(default_factory=_Usage)


class DeepSeekModel(OllamaModel):
    """Minimal live DeepSeek adapter using its OpenAI-compatible endpoint.

    Conversation assembly, prompt trust labels, and tool-result handling reuse
    the already-proven Ollama adapter implementation. Only the provider wire
    format differs. Credentials remain confined to the HTTP Authorization
    header and are never included in repr, context, events, or telemetry.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        api_key: str,
        tools: tuple[ModelToolSpec, ...],
        template: PromptTemplate,
        model: str = "deepseek-v4-pro",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 300.0,
        context_window_tokens: int = 1_000_000,
        input_cost_usd_per_million: float = 1.32,
        output_cost_usd_per_million: float = 3.96,
        thinking: bool = True,
        reasoning_effort: str = "high",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            msg = "DeepSeek API key cannot be empty"
            raise ValueError(msg)
        if reasoning_effort not in {"low", "high", "max"}:
            msg_2 = "DeepSeek reasoning effort must be low, high, or max"
            raise ValueError(msg_2)
        rates = (input_cost_usd_per_million, output_cost_usd_per_million)
        if any(not math.isfinite(rate) or rate < 0 for rate in rates):
            msg_3 = "DeepSeek cost rates must be finite and non-negative"
            raise ValueError(msg_3)
        # The parent supplies the shared prompt/conversation/tool-call machinery.
        # Its temporary local capability record is replaced before construction
        # returns and can never cross the adapter boundary.
        super().__init__(
            model=model,
            tools=tools,
            template=template,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        self._capabilities = ModelCapabilities(
            provider="deepseek",
            model=model,
            supports_tool_calls=True,
            context_window_tokens=context_window_tokens,
            input_cost_usd_per_million=input_cost_usd_per_million,
            output_cost_usd_per_million=output_cost_usd_per_million,
        )
        self._thinking = thinking
        self._reasoning_effort = reasoning_effort

    def propose_action(self, context: ModelContext) -> ModelTurn:
        rendered = self._render(context)
        messages = self._conversation(context, rendered)
        pending_call_id: str | None = None
        for message in messages:
            calls = message.get("tool_calls")
            if message.get("role") == "assistant" and isinstance(calls, list) and calls:
                call = cast(dict[str, object], calls[0])
                pending_call_id = str(call["id"])
            if message.get("role") == "tool" and pending_call_id is not None:
                message["tool_call_id"] = pending_call_id
        self._turn_count += 1
        payload: dict[str, object] = {
            "model": self._capabilities.model,
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
            "stream": False,
            "parallel_tool_calls": False,
            "thinking": {"type": "enabled" if self._thinking else "disabled"},
            "reasoning_effort": self._reasoning_effort,
        }
        if not self._thinking:
            payload["tool_choice"] = "required"
        response = self._post_deepseek(payload)
        if len(response.choices) != 1:
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT,
                "MODEL_INVALID_RESPONSE",
                f"model returned {len(response.choices)} choices, expected exactly one",
            )
        choice = response.choices[0]
        if choice.finish_reason not in {None, "tool_calls"}:
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT,
                "MODEL_INVALID_RESPONSE",
                _bounded(f"model stopped without a tool call: {choice.finish_reason}"),
            )
        # Normalize the OpenAI argument JSON string into the parent's strict
        # string-to-string action parser.
        normalized_calls: list[dict[str, object]] = []
        # DeepSeek may emit parallel calls even when explicitly disabled. The
        # runtime is intentionally single-action-per-cycle, so admit only the
        # first proposal; remaining calls grant no authority and are ignored.
        for call in (choice.message.tool_calls or [])[:1]:
            try:
                arguments = json.loads(call.function.arguments)
            except json.JSONDecodeError as exc:
                raise ModelTurnError(
                    ModelFailureClass.TRANSIENT,
                    "MODEL_INVALID_RESPONSE",
                    "model tool arguments were not valid JSON",
                ) from exc
            if not isinstance(arguments, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in cast(dict[object, object], arguments).items()
            ):
                raise ModelTurnError(
                    ModelFailureClass.TRANSIENT,
                    "MODEL_INVALID_RESPONSE",
                    "model tool arguments must be a string-to-string object",
                )
            normalized_calls.append(
                {"function": {"name": call.function.name, "arguments": arguments}}
            )
        normalized = ChatResponse.model_validate(
            {
                "message": {
                    "role": choice.message.role,
                    "content": choice.message.content or "",
                    "tool_calls": normalized_calls,
                },
                "done": True,
            }
        )
        action = self._parse_action(context, normalized)
        history = self._conversations.get(context.run_id)
        if history is not None:
            if self._thinking:
                history[-1]["reasoning_content"] = choice.message.reasoning_content or ""
            assistant_call = history[-1]["tool_calls"]
            if isinstance(assistant_call, list) and assistant_call:
                calls = cast(list[dict[str, object]], assistant_call)
                function = cast(dict[str, object], calls[0]["function"])
                function["arguments"] = json.dumps(function["arguments"], separators=(",", ":"))
        usage = response.usage
        cost = (
            usage.prompt_tokens * self._capabilities.input_cost_usd_per_million
            + usage.completion_tokens * self._capabilities.output_cost_usd_per_million
        ) / 1_000_000
        return ModelTurn(
            action=action,
            usage=UsageDelta(
                cost_usd=cost,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cached_input_tokens=usage.prompt_cache_hit_tokens,
            ),
        )

    def _post_deepseek(self, payload: dict[str, object]) -> _Response:
        try:
            raw = self._client.post(_CHAT_PATH, json=payload)
        except httpx.TimeoutException as exc:
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_TIMEOUT", "provider request timed out"
            ) from exc
        except httpx.DecodingError as exc:
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT,
                "MODEL_INVALID_RESPONSE",
                "provider response body could not be decoded",
            ) from exc
        except httpx.RequestError as exc:
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT,
                "MODEL_UNAVAILABLE",
                _bounded(f"provider transport failure: {type(exc).__name__}"),
            ) from exc
        if raw.status_code != 200:
            if raw.status_code == 400:
                try:
                    detail = str(raw.json().get("error", {}).get("message", ""))
                except (json.JSONDecodeError, AttributeError):
                    detail = ""
                summary = "provider rejected the request (HTTP 400)"
                if detail:
                    summary = f"{summary}: {_bounded(detail)}"
                raise ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_REQUEST_INVALID", summary)
            self._raise_for_status(raw.status_code)
        try:
            return _Response.model_validate(raw.json())
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT,
                "MODEL_INVALID_RESPONSE",
                "provider response violated the chat schema",
            ) from exc


def _bounded(text: str) -> str:
    sanitized = "".join(char if char.isprintable() else "?" for char in text)
    return sanitized if len(sanitized) <= _SUMMARY_BUDGET else sanitized[:197] + "..."
