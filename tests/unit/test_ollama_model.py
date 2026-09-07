"""Unit tests for the live Ollama model adapter (hermetic, mocked transport)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from loopforge.adapters.ollama_model import OllamaModel
from loopforge.domain.context import (
    ContextItem,
    ContextSource,
    ModelContext,
    ModelRole,
)
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.security import TrustClass
from loopforge.domain.tooling import DataSensitivity
from loopforge.domain.types import ContextItemId, RunId
from loopforge.ports.model import (
    ModelFailureClass,
    ModelToolSpec,
    ModelTurnError,
)

NOW = datetime(2026, 8, 28, tzinfo=UTC)
RUN = RunId("ollama-run")
TEMPLATE = default_controller_template()
TOOLS = (
    ModelToolSpec(
        name="read_file",
        description="Read a workspace file.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ),
    ModelToolSpec(
        name="run_tests",
        description="Run the predefined test command.",
        parameters={"type": "object", "properties": {}},
    ),
)


def _context(*, template_ref: object = "default") -> ModelContext:
    item = ContextItem(
        item_id=ContextItemId(f"{RUN}:objective"),
        content="Repair the adder regression.",
        trust=TrustClass.AUTHORIZED_HUMAN,
        source=ContextSource(origin=TrustClass.AUTHORIZED_HUMAN, reference="ref:objective"),
        sensitivity=DataSensitivity.INTERNAL,
        created_at=NOW,
    )
    ref = TEMPLATE.reference() if template_ref == "default" else template_ref
    return ModelContext(
        run_id=RUN,
        items=(item,),
        assembled_at=NOW,
        role=ModelRole.CONTROLLER,
        prompt_template=ref,  # type: ignore[arg-type]
    )


def _item(key: str, content: str, trust: TrustClass) -> ContextItem:
    return ContextItem(
        item_id=ContextItemId(f"{RUN}:{key}"),
        content=content,
        trust=trust,
        source=ContextSource(origin=trust, reference=f"ref:{key}"),
        sensitivity=DataSensitivity.INTERNAL,
        created_at=NOW,
    )


def _context_with(*items: ContextItem) -> ModelContext:
    return ModelContext(
        run_id=RUN,
        items=tuple(items),
        assembled_at=NOW,
        role=ModelRole.CONTROLLER,
        prompt_template=TEMPLATE.reference(),
    )


def _chat_payload(
    *,
    tool_calls: object = None,
    done: bool = True,
    prompt_tokens: int = 190,
    output_tokens: int = 30,
) -> dict[str, object]:
    if tool_calls is None:
        tool_calls = [
            {"function": {"name": "read_file", "arguments": {"path": "adder.py"}}, "id": "call_1"}
        ]
    return {
        "model": "devstral-small-2:latest",
        "message": {"role": "assistant", "content": "", "tool_calls": tool_calls},
        "done": done,
        "prompt_eval_count": prompt_tokens,
        "eval_count": output_tokens,
        "total_duration": 37_000_000_000,
    }


def _model(
    handler: httpx.MockTransport | None = None,
    *,
    api_key: str | None = None,
    capabilities: ModelCapabilities | None = None,
) -> OllamaModel:
    return OllamaModel(
        model="devstral-small-2:latest",
        tools=TOOLS,
        template=TEMPLATE,
        api_key=api_key,
        capabilities=capabilities,
        transport=handler,
    )


def _responding(payload: object, *, status: int = 200) -> httpx.MockTransport:
    body = payload if isinstance(payload, str) else json.dumps(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


# --- Happy path ---------------------------------------------------------------


def test_propose_action_parses_tool_call_and_usage() -> None:
    model = _model(_responding(_chat_payload()))
    turn = model.propose_action(_context())
    assert turn.action.tool_name == "read_file"
    assert turn.action.arguments == {"path": "adder.py"}
    assert turn.action.action_id == f"{RUN}:model-turn-1"
    assert turn.usage.input_tokens == 190
    assert turn.usage.output_tokens == 30
    assert turn.usage.cost_usd == 0.0


def test_action_id_increments_per_turn() -> None:
    model = _model(_responding(_chat_payload()))
    first = model.propose_action(_context())
    second = model.propose_action(_context())
    assert first.action.action_id.endswith("model-turn-1")
    assert second.action.action_id.endswith("model-turn-2")


def test_usage_cost_uses_capability_rates() -> None:
    capabilities = ModelCapabilities(
        provider="ollama",
        # Identity must match the wired model (PACS-012 hardening).
        model="devstral-small-2:latest",
        supports_tool_calls=True,
        context_window_tokens=8192,
        input_cost_usd_per_million=10.0,
        output_cost_usd_per_million=100.0,
    )
    model = _model(_responding(_chat_payload()), capabilities=capabilities)
    turn = model.propose_action(_context())
    assert turn.usage.cost_usd == (190 * 10.0 + 30 * 100.0) / 1_000_000


def test_missing_token_counts_default_to_zero() -> None:
    payload = _chat_payload()
    del payload["prompt_eval_count"]
    del payload["eval_count"]
    model = _model(_responding(payload))
    turn = model.propose_action(_context())
    assert turn.usage.input_tokens == 0
    assert turn.usage.output_tokens == 0


def test_request_shape_renders_versioned_prompt_and_tools() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=json.dumps(_chat_payload()))

    model = _model(httpx.MockTransport(handler))
    model.propose_action(_context())
    (request,) = captured
    assert request.url.path == "/api/chat"
    body = json.loads(request.content)
    assert body["model"] == "devstral-small-2:latest"
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.0}
    system, user = body["messages"]
    assert system["role"] == "system"
    assert "controller model" in system["content"]
    assert user["role"] == "user"
    assert "(authorized_human) Repair the adder regression." in user["content"]
    tool_names = [tool["function"]["name"] for tool in body["tools"]]
    assert tool_names == ["read_file", "run_tests"]


def test_observation_arrives_as_tool_result_next_turn() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=json.dumps(_chat_payload()))

    model = _model(httpx.MockTransport(handler))
    model.propose_action(_context())
    follow_up = _context_with(
        _item("objective", "Repair the adder regression.", TrustClass.AUTHORIZED_HUMAN),
        _item("observation", "def add(l, r):\n    return l - r", TrustClass.UNTRUSTED_CONTENT),
        _item("verifier", "Verification failed: tests failed", TrustClass.RUNTIME_POLICY),
    )
    model.propose_action(follow_up)
    second = json.loads(captured[1].content)
    roles = [message["role"] for message in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assistant = second["messages"][2]
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    tool = second["messages"][3]
    assert tool["name"] == "read_file"
    # Trust labels travel with tool results: untrusted repository output must
    # be distinguishable from runtime-owned observations at the boundary.
    assert "(untrusted_content)" in tool["content"]
    assert "return l - r" in tool["content"]
    update = second["messages"][4]
    assert "(runtime_policy) Verification failed: tests failed" in update["content"]


def test_rejected_proposal_yields_explicit_empty_tool_result() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=json.dumps(_chat_payload()))

    model = _model(httpx.MockTransport(handler))
    model.propose_action(_context())
    # No new observation: the runtime rejected the proposal before execution.
    model.propose_action(_context())
    second = json.loads(captured[1].content)
    tool = second["messages"][3]
    assert tool["role"] == "tool"
    assert "No new observation was recorded" in tool["content"]


def test_drop_conversation_starts_fresh() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=json.dumps(_chat_payload()))

    model = _model(httpx.MockTransport(handler))
    model.propose_action(_context())
    model.drop_conversation(RUN)
    model.propose_action(_context())
    second = json.loads(captured[1].content)
    assert [message["role"] for message in second["messages"]] == ["system", "user"]


# --- Secret isolation ---------------------------------------------------------


def test_api_key_stays_in_headers_only() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=json.dumps(_chat_payload()))

    model = _model(httpx.MockTransport(handler), api_key="top-secret-token")
    model.propose_action(_context())
    (request,) = captured
    assert request.headers["Authorization"] == "Bearer top-secret-token"
    assert "top-secret-token" not in request.content.decode()
    assert "top-secret-token" not in repr(model)


def test_no_authorization_header_without_api_key() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=json.dumps(_chat_payload()))

    model = _model(httpx.MockTransport(handler))
    model.propose_action(_context())
    (request,) = captured
    assert "Authorization" not in request.headers


# --- Malformed provider responses ----------------------------------------------


def test_non_json_response_fails_explicitly() -> None:
    model = _model(_responding("not json at all"))
    with pytest.raises(ModelTurnError, match="not valid JSON") as excinfo:
        model.propose_action(_context())
    assert excinfo.value.failure_class is ModelFailureClass.TRANSIENT
    assert excinfo.value.reason_code == "MODEL_INVALID_RESPONSE"


def test_schema_violation_fails_explicitly() -> None:
    model = _model(_responding({"done": True}))
    with pytest.raises(ModelTurnError, match="chat schema") as excinfo:
        model.propose_action(_context())
    assert excinfo.value.reason_code == "MODEL_INVALID_RESPONSE"


def test_incomplete_generation_fails_explicitly() -> None:
    model = _model(_responding(_chat_payload(done=False)))
    with pytest.raises(ModelTurnError, match="not a completed generation"):
        model.propose_action(_context())


_BAD_CALL_SHAPES: list[tuple[list[dict[str, object]], str]] = [
    ([], "0 tool actions"),
    (
        [
            {"function": {"name": "read_file", "arguments": {"path": "a"}}},
            {"function": {"name": "run_tests", "arguments": {}}},
        ],
        "2 tool actions",
    ),
    ([{"function": {"name": "  ", "arguments": {}}}], "empty tool name"),
]


@pytest.mark.parametrize(("tool_calls", "match"), _BAD_CALL_SHAPES)
def test_wrong_tool_call_shape_fails_explicitly(
    tool_calls: list[dict[str, object]], match: str
) -> None:
    model = _model(_responding(_chat_payload(tool_calls=tool_calls)))
    with pytest.raises(ModelTurnError, match=match) as excinfo:
        model.propose_action(_context())
    assert excinfo.value.failure_class is ModelFailureClass.TRANSIENT


def test_non_string_argument_value_fails_explicitly() -> None:
    calls = [{"function": {"name": "read_file", "arguments": {"path": 42}}}]
    model = _model(_responding(_chat_payload(tool_calls=calls)))
    with pytest.raises(ModelTurnError, match="chat schema"):
        model.propose_action(_context())


def test_negative_token_counts_fail_explicitly() -> None:
    model = _model(_responding(_chat_payload(prompt_tokens=-1)))
    with pytest.raises(ModelTurnError, match="chat schema"):
        model.propose_action(_context())


def test_prompt_template_mismatch_fails_closed() -> None:
    model = _model(_responding(_chat_payload()))
    with pytest.raises(ModelTurnError, match="prompt template") as excinfo:
        model.propose_action(_context(template_ref=None))
    assert excinfo.value.reason_code == "MODEL_PROMPT_TEMPLATE_MISMATCH"


# --- Provider failure normalization ---------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_permanent(status: int) -> None:
    model = _model(_responding("{}", status=status))
    with pytest.raises(ModelTurnError, match="credentials") as excinfo:
        model.propose_action(_context())
    assert excinfo.value.failure_class is ModelFailureClass.PERMANENT
    assert excinfo.value.reason_code == "MODEL_AUTHENTICATION"


def test_missing_model_is_permanent() -> None:
    model = _model(_responding("{}", status=404))
    with pytest.raises(ModelTurnError, match="does not serve") as excinfo:
        model.propose_action(_context())
    assert excinfo.value.reason_code == "MODEL_NOT_FOUND"


@pytest.mark.parametrize("status", [400, 418])
def test_rejected_request_is_permanent(status: int) -> None:
    model = _model(_responding("{}", status=status))
    with pytest.raises(ModelTurnError, match="rejected the request") as excinfo:
        model.propose_action(_context())
    assert excinfo.value.reason_code == "MODEL_REQUEST_INVALID"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_unavailable_provider_is_transient(status: int) -> None:
    model = _model(_responding("{}", status=status))
    with pytest.raises(ModelTurnError, match="unavailable") as excinfo:
        model.propose_action(_context())
    assert excinfo.value.failure_class is ModelFailureClass.TRANSIENT
    assert excinfo.value.reason_code == "MODEL_UNAVAILABLE"


def test_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "slow provider"
        raise httpx.ConnectTimeout(msg)

    model = _model(httpx.MockTransport(handler))
    with pytest.raises(ModelTurnError, match="timed out") as excinfo:
        model.propose_action(_context())
    assert excinfo.value.failure_class is ModelFailureClass.TRANSIENT
    assert excinfo.value.reason_code == "MODEL_TIMEOUT"


def test_transport_failure_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg)

    model = _model(httpx.MockTransport(handler))
    with pytest.raises(ModelTurnError, match="transport failure") as excinfo:
        model.propose_action(_context())
    assert excinfo.value.failure_class is ModelFailureClass.TRANSIENT
    assert excinfo.value.reason_code == "MODEL_UNAVAILABLE"


# --- Metadata and construction ---------------------------------------------------


def test_default_capabilities() -> None:
    model = _model(_responding(_chat_payload()))
    capabilities = model.capabilities
    assert capabilities.provider == "ollama"
    assert capabilities.model == "devstral-small-2:latest"
    assert capabilities.supports_tool_calls is True
    assert capabilities.input_cost_usd_per_million == 0.0


def test_constructor_validation() -> None:
    with pytest.raises(ValueError, match="model name cannot be empty"):
        OllamaModel(model=" ", tools=TOOLS, template=TEMPLATE)
    with pytest.raises(ValueError, match="non-empty tool catalog"):
        OllamaModel(model="m", tools=(), template=TEMPLATE)
    with pytest.raises(ValueError, match="names must be unique"):
        OllamaModel(model="m", tools=(TOOLS[0], TOOLS[0]), template=TEMPLATE)
    with pytest.raises(ValueError, match="timeout must be positive"):
        OllamaModel(model="m", tools=TOOLS, template=TEMPLATE, timeout_seconds=0)


def test_close_releases_the_http_client() -> None:
    model = _model(_responding(_chat_payload()))
    model.close()
    with pytest.raises(RuntimeError):
        model.propose_action(_context())
