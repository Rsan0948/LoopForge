"""Hermetic contract tests for the minimal DeepSeek adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from loopforge.adapters.deepseek_model import DeepSeekModel
from loopforge.domain.context import ContextItem, ContextSource, ModelContext, ModelRole
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.security import TrustClass
from loopforge.domain.tooling import DataSensitivity
from loopforge.domain.types import ContextItemId, RunId
from loopforge.ports.model import ModelFailureClass, ModelToolSpec, ModelTurnError

RUN = RunId("deepseek-run")
TEMPLATE = default_controller_template()
TOOLS = (
    ModelToolSpec(
        name="read_file",
        description="Read a file.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ),
)


def _context() -> ModelContext:
    now = datetime(2026, 8, 28, tzinfo=UTC)
    return ModelContext(
        run_id=RUN,
        items=(
            ContextItem(
                item_id=ContextItemId("objective"),
                content="Repair the bug.",
                trust=TrustClass.AUTHORIZED_HUMAN,
                source=ContextSource(origin=TrustClass.AUTHORIZED_HUMAN, reference="objective"),
                sensitivity=DataSensitivity.INTERNAL,
                created_at=now,
            ),
        ),
        assembled_at=now,
        role=ModelRole.CONTROLLER,
        prompt_template=TEMPLATE.reference(),
    )


def _payload(*, arguments: str = '{"path":"adder.py"}') -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"function": {"name": "read_file", "arguments": arguments}}],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 30,
        },
    }


def test_deepseek_parses_action_usage_and_request() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_payload())

    model = DeepSeekModel(
        api_key="secret-key",
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(handler),
    )
    turn = model.propose_action(_context())
    assert turn.action.tool_name == "read_file"
    assert turn.action.arguments == {"path": "adder.py"}
    assert turn.usage.input_tokens == 100
    assert turn.usage.output_tokens == 20
    assert turn.usage.cached_input_tokens == 30
    assert turn.usage.cost_usd == (100 * 1.32 + 20 * 3.96) / 1_000_000
    request = captured[0]
    assert request.url.path == "/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-key"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-v4-pro"
    assert "tool_choice" not in body
    assert body["thinking"] == {"type": "enabled"}
    model.close()


def test_deepseek_rejects_non_string_tool_arguments() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_payload(arguments='{"line":1}'))
    )
    model = DeepSeekModel(api_key="key", tools=TOOLS, template=TEMPLATE, transport=transport)
    with pytest.raises(ModelTurnError, match="string-to-string") as raised:
        model.propose_action(_context())
    assert raised.value.failure_class is ModelFailureClass.TRANSIENT


def test_deepseek_normalizes_authentication_failure() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    model = DeepSeekModel(api_key="key", tools=TOOLS, template=TEMPLATE, transport=transport)
    with pytest.raises(ModelTurnError) as raised:
        model.propose_action(_context())
    assert raised.value.reason_code == "MODEL_AUTHENTICATION"


def test_deepseek_requires_credential() -> None:
    with pytest.raises(ValueError, match="API key"):
        DeepSeekModel(api_key=" ", tools=TOOLS, template=TEMPLATE)
