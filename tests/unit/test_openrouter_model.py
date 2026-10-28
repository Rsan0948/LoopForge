"""Hermetic OpenRouter native action, history and usage normalization."""

import json

import httpx
import pytest
from test_deepseek_model import TEMPLATE, TOOLS, _context  # pyright: ignore[reportPrivateUsage]

from loopforge.adapters.openrouter_model import OpenRouterModel
from loopforge.domain.routing import ModelCapabilities
from loopforge.ports.model import ModelFailureClass, ModelTurnError

CAPABILITIES = ModelCapabilities(
    provider="openrouter",
    model="anthropic/claude-haiku-5.5",
    supports_tool_calls=True,
    context_window_tokens=1_000_000,
    input_cost_usd_per_million=1,
    output_cost_usd_per_million=2.5,
)


def payload(arguments: str = '{"path":"adder.py"}') -> dict[str, object]:
    return {
        "model": CAPABILITIES.model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Inspect the source.",
                    "reasoning_details": [{"type": "reasoning.encrypted", "data": "signature"}],
                    "tool_calls": [{"function": {"name": "read_file", "arguments": arguments}}],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.00002},
    }


def test_native_action_history_actual_usage_and_credential_exclusion() -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert b"test-secret" not in request.content
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=payload())

    model = OpenRouterModel(
        api_key="test-secret",
        capabilities=CAPABILITIES,
        model=CAPABILITIES.model,
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(respond),
        max_output_tokens=8192,
    )
    try:
        turn = model.propose_action(_context())
        assert turn.action.arguments == {"path": "adder.py"}
        assert turn.usage.cost_usd == 0.00002
        assert model.capabilities.provider == "openrouter"
        model.propose_action(_context())
        wire = json.dumps(requests[-1])
        assert '"tool_call_id": "call_1"' in wire
        assert '"data": "signature"' in wire
        assert '"arguments": "{\\"path\\": \\"adder.py\\"}"' in wire
        assert requests[0]["tool_choice"] == "required"
        assert requests[0]["cache_control"] == {"type": "ephemeral"}
    finally:
        model.close()


@pytest.mark.parametrize("cached", [None, 0, 30, 1000])
def test_cache_credit_uses_provider_usage_even_for_an_unchanged_prefix(cached: int | None) -> None:
    response = payload()
    if cached is not None:
        response["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cost": 0.00002,
            "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": 10},
        }
    model = OpenRouterModel(
        api_key="offline",
        capabilities=CAPABILITIES,
        model=CAPABILITIES.model,
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    )
    try:
        for _ in range(2):
            assert model.propose_action(_context()).usage.cached_input_tokens == min(
                cached or 0, 100
            )
    finally:
        model.close()


@pytest.mark.parametrize("arguments", ['{"path":42}', "not-json"])
def test_malformed_action_retains_paid_usage(arguments: str) -> None:
    model = OpenRouterModel(
        api_key="secret",
        capabilities=CAPABILITIES,
        model=CAPABILITIES.model,
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload(arguments))),
    )
    try:
        with pytest.raises(ModelTurnError) as error:
            model.propose_action(_context())
        assert error.value.usage is not None
        assert error.value.usage.cost_usd == 0.00002
    finally:
        model.close()


@pytest.mark.parametrize("cached", [-1, "30", True])
def test_invalid_cache_usage_never_erases_known_billed_tokens(cached: object) -> None:
    response = payload()
    response["usage"] = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cost": 0.00002,
        "prompt_tokens_details": {"cached_tokens": cached},
    }
    model = OpenRouterModel(
        api_key="offline",
        capabilities=CAPABILITIES,
        model=CAPABILITIES.model,
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    )
    try:
        with pytest.raises(ModelTurnError) as error:
            model.propose_action(_context())
        assert error.value.usage is not None
        assert error.value.usage.input_tokens == 100
        assert error.value.usage.cached_input_tokens == 0
        assert error.value.usage.cost_usd == 0.00002
    finally:
        model.close()


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (429, ModelFailureClass.TRANSIENT),
        (401, ModelFailureClass.PERMANENT),
        (503, ModelFailureClass.PERMANENT),
    ],
)
def test_provider_failure_is_classified_without_echoing_secrets(
    status: int, category: ModelFailureClass
) -> None:
    model = OpenRouterModel(
        api_key="secret",
        capabilities=CAPABILITIES,
        model=CAPABILITIES.model,
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text="secret")),
    )
    try:
        with pytest.raises(ModelTurnError) as error:
            model.propose_action(_context())
        assert error.value.failure_class == category
        assert "secret" not in str(error.value)
    finally:
        model.close()
