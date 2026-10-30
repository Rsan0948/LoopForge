"""OpenRouter wire adapter over the existing native-tool conversation machinery."""

from __future__ import annotations

import json
from typing import cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loopforge.adapters.ollama_model import ChatResponse, OllamaModel
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.types import UsageDelta
from loopforge.ports.model import ModelFailureClass, ModelTurnError


class Function(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    name: str
    arguments: str


class ToolCall(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    function: Function


class Message(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list[ToolCall])
    reasoning_details: list[dict[str, object]] | None = None


class Choice(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    message: Message
    finish_reason: str | None = None


class PromptDetails(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class BasicUsage(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost: float = Field(ge=0, allow_inf_nan=False)


class ProviderUsage(BasicUsage):
    prompt_tokens_details: PromptDetails | None = None


class UsageEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    usage: BasicUsage


class Response(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    model: str
    choices: list[Choice]
    usage: ProviderUsage
    id: str = Field(default="", max_length=300)


class OpenRouterModel(OllamaModel):
    """Reuse prompt trust, history, compaction and strict runtime action parsing.

    Provider selection is pinned by trusted composition. A supplied transport can
    enforce a shared paid ledger before requests, without a product import here.
    """

    _PROVIDER = "openrouter"

    def __init__(
        self,
        *,
        api_key: str | None,
        capabilities: ModelCapabilities | None,
        reasoning_tokens: int | None = None,
        **kwargs: object,
    ) -> None:
        if (
            not api_key
            or any(c.isspace() for c in api_key)
            or capabilities is None
            or capabilities.provider != "openrouter"
        ):
            msg = "OpenRouter requires a credential and code-owned model capabilities"
            raise ValueError(msg)
        if kwargs.get("model") != capabilities.model:
            msg = "OpenRouter model identity must match its capability contract"
            raise ValueError(msg)
        # A reasoning allowance inside the output limit, so long reasoning leaves room for
        # the action. Probed 2026-10-30 (DeepSeek V4.1 Flash at DeepInfra): with one shared
        # 4,096 limit reasoning took 3,823 tokens and no tool call came back; capped at 512
        # inside 1,536 it took 922 and the call came back. The cap is soft, so the rest of
        # the output limit must cover overshoot as well as the action.
        output_limit = kwargs.get("max_output_tokens")
        if reasoning_tokens is not None and (
            type(reasoning_tokens) is not int
            or reasoning_tokens <= 0
            or type(output_limit) is not int
            or reasoning_tokens >= output_limit
        ):
            msg = "reasoning_tokens must be a positive integer below max_output_tokens"
            raise ValueError(msg)
        self._reasoning_tokens = reasoning_tokens
        # The parent constructor's rich conversation options remain its contract.
        super().__init__(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            capabilities=capabilities,
            **kwargs,  # type: ignore[arg-type]
        )
        self._charged = 0.0
        self._reasoning_details: list[dict[str, object]] | None = None

    def _cost(self, response: ChatResponse) -> float:
        del response
        return self._charged

    def _parse_action(self, context: ModelContext, response: ChatResponse) -> ActionProposal:
        action = super()._parse_action(context, response)
        history = self._conversations.get(context.run_id)
        if history and self._reasoning_details is not None:
            history[-1]["reasoning_details"] = self._reasoning_details
        return action

    def _post(self, payload: dict[str, object]) -> ChatResponse:  # noqa: PLR0912, PLR0915 - provider wire normalization
        messages: list[dict[str, object]] = []
        pending: str | None = None
        for source in cast("list[dict[str, object]]", payload["messages"]):
            message = {
                key: value
                for key, value in source.items()
                if key in {"role", "content", "reasoning_details"}
            }
            if message["role"] == "system" and messages:
                message["role"] = "user"
            calls = source.get("tool_calls")
            if isinstance(calls, list) and calls:
                call = cast("dict[str, object]", calls[0])
                function = dict(cast("dict[str, object]", call["function"]))
                arguments = function["arguments"]
                function["arguments"] = (
                    arguments if isinstance(arguments, str) else json.dumps(arguments)
                )
                pending = str(call["id"])
                message["tool_calls"] = [{"id": pending, "type": "function", "function": function}]
            if message["role"] == "tool":
                if pending is None:
                    msg = "Tool observation has no matching assistant call"
                    raise ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_REQUEST_INVALID", msg)
                message["tool_call_id"] = pending
                pending = None
            messages.append(message)
        body = {
            "model": self.capabilities.model,
            "messages": messages,
            "tools": payload["tools"],
            "tool_choice": "required",
            "max_tokens": self._max_output_tokens or 8192,
            "stream": False,
            "provider": {
                "allow_fallbacks": False,
                "max_price": {
                    "prompt": self.capabilities.input_cost_usd_per_million / 2,
                    "completion": self.capabilities.output_cost_usd_per_million,
                },
            },
        }
        # Anthropic requires explicit cache enablement. A five-minute cache write
        # is bounded by the existing conservative uncached reservation; hits are
        # counted only from usage, never from this request hint.
        if self.capabilities.model.startswith("anthropic/"):
            body["cache_control"] = {"type": "ephemeral"}
        if self._reasoning_tokens is not None:
            # A turn the parent marked think=False (after output ran out without an
            # action) reaches the provider; without a reasoning allowance the flag is
            # not sent, since some endpoints refuse disabling mandatory reasoning.
            body["reasoning"] = (
                {"enabled": False}
                if payload.get("think") is False
                else {"max_tokens": self._reasoning_tokens}
            )
        if self.request_recorder is not None:
            self.request_recorder(body)
        try:
            self.request_tracker.begin(self.request_timeout_seconds, "controller")
            try:
                raw = self._client.post(
                    "chat/completions", json=body, timeout=self.request_timeout_seconds
                )
            finally:
                self.request_tracker.end()
        except httpx.RequestError as exc:
            # The exception class is code-owned and carries no request data; without it a
            # live failure could not be diagnosed.
            msg = f"OpenRouter transport failed ({type(exc).__name__}); billing may be unsettled"
            # Transient: the runtime retries the turn with bounded backoff. Any paid
            # reservation stays held at its maximum, so a retry spends only within the cap.
            # Live 2026-10-28: ReadError ended attempts that were otherwise progressing.
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_BILLING_UNCERTAIN", msg
            ) from exc
        if raw.status_code != 200:
            msg = f"OpenRouter request failed (HTTP {raw.status_code})"
            if raw.status_code == 429:
                raise ModelTurnError(ModelFailureClass.TRANSIENT, "MODEL_UNAVAILABLE", msg)
            if raw.status_code >= 500:
                raise ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_BILLING_UNCERTAIN", msg)
            raise ModelTurnError(ModelFailureClass.PERMANENT, "MODEL_REQUEST_INVALID", msg)
        known_usage: UsageDelta | None = None
        try:
            billing = UsageEnvelope.model_validate_json(raw.content).usage
            known_usage = UsageDelta(
                cost_usd=billing.cost,
                input_tokens=billing.prompt_tokens,
                output_tokens=billing.completion_tokens,
            )
            response = Response.model_validate_json(raw.content)
        except ValidationError as exc:
            msg = "OpenRouter returned an invalid response envelope"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_INVALID_RESPONSE", msg, usage=known_usage
            ) from exc
        self._charged = response.usage.cost
        details = response.usage.prompt_tokens_details
        cached = min(details.cached_tokens, response.usage.prompt_tokens) if details else 0
        usage = UsageDelta(
            cost_usd=self._charged,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            cached_input_tokens=cached,
        )
        self.request_tracker.record_provider_usage(
            usage,
            response_id=response.id,
            cache_write_tokens=details.cache_write_tokens if details else 0,
        )
        if response.model != self.capabilities.model or len(response.choices) != 1:
            msg = "OpenRouter response identity or choice count differs from the request"
            raise ModelTurnError(
                ModelFailureClass.PERMANENT, "MODEL_INVALID_RESPONSE", msg, usage=usage
            )
        try:
            choice = response.choices[0]
            self._reasoning_details = choice.message.reasoning_details
            return ChatResponse.model_validate(
                {
                    "message": {
                        "role": choice.message.role,
                        "content": choice.message.content or "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": c.function.name,
                                    "arguments": json.loads(c.function.arguments),
                                }
                            }
                            for c in choice.message.tool_calls
                        ],
                    },
                    "done": True,
                    "prompt_eval_count": response.usage.prompt_tokens,
                    "eval_count": response.usage.completion_tokens,
                    "prompt_eval_cached_count": cached,
                }
            )
        except (ValueError, TypeError) as exc:
            msg = "OpenRouter tool arguments violate the native action schema"
            raise ModelTurnError(
                ModelFailureClass.TRANSIENT, "MODEL_INVALID_RESPONSE", msg, usage=usage
            ) from exc
