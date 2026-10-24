"""Unit tests for the live Ollama model adapter (hermetic, mocked transport)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest

from loopforge.adapters.ollama_model import OllamaModel
from loopforge.domain.admission import InputMeasurementBasis
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


@pytest.mark.parametrize(
    "selected", [frozenset({"read_file"}), frozenset[str](), frozenset({"new_tool"})]
)
def test_code_owned_tool_filter_only_narrows_registered_catalog(selected: frozenset[str]) -> None:
    requests: list[bytes] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="test",
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(response),
        tool_filter=lambda _: selected,
    )
    try:
        if selected == frozenset({"read_file"}):
            assert model.propose_action(_context()).action.tool_name == "read_file"
            assert [t["function"]["name"] for t in json.loads(requests[0])["tools"]] == [
                "read_file"
            ]
        else:
            with pytest.raises(ModelTurnError, match="nonempty registered subset"):
                model.propose_action(_context())
            assert not requests
    finally:
        model.close()


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


def test_explicit_generation_settings_are_sent_without_changing_default() -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        max_output_tokens=4096,
        thinking=False,
        transport=httpx.MockTransport(respond),
    )
    model.propose_action(_context())
    assert requests[0]["think"] is False
    assert requests[0]["options"] == {"temperature": 0.0, "num_predict": 4096}


@pytest.mark.parametrize(
    ("output_tokens", "calls", "fallback"),
    [
        (64, list[dict[str, object]](), True),
        (63, list[dict[str, object]](), False),
        (64, [dict[str, object](function={"name": "read_file", "arguments": {}})] * 2, False),
    ],
)
def test_exhausted_actionless_thinking_falls_back_without_expanding_budget(
    output_tokens: int, calls: list[dict[str, object]], fallback: bool
) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        payload = (
            _chat_payload(tool_calls=calls, output_tokens=output_tokens)
            if len(requests) == 1
            else _chat_payload()
        )
        return httpx.Response(200, json=payload)

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        thinking=True,
        max_output_tokens=64,
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(ModelTurnError) as exc:
        model.propose_action(_context())
    assert exc.value.usage is not None
    assert exc.value.usage.output_tokens == output_tokens
    assert ("disable thinking" in exc.value.summary) is fallback
    assert model.propose_action(_context()).action.tool_name == "read_file"
    assert requests[0]["think"] is True
    assert requests[1]["think"] is (not fallback)
    assert requests[0]["options"] == requests[1]["options"]
    current = _context()
    other = ModelContext(
        run_id=RunId("other-run"),
        items=current.items,
        assembled_at=NOW,
        role=current.role,
        prompt_template=current.prompt_template,
    )
    model.propose_action(other)
    assert requests[2]["think"] is True


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_output_token_limit_denied(value: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        OllamaModel(
            model="m",
            tools=TOOLS,
            template=TEMPLATE,
            max_output_tokens=value,  # type: ignore[arg-type]
        )


def test_invalid_thinking_option_denied() -> None:
    with pytest.raises(ValueError, match="boolean"):
        OllamaModel(model="m", tools=TOOLS, template=TEMPLATE, thinking="false")  # type: ignore[arg-type]


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


def test_missing_token_counts_fail_closed() -> None:
    payload = _chat_payload()
    del payload["prompt_eval_count"]
    del payload["eval_count"]
    model = _model(_responding(payload))
    with pytest.raises(ModelTurnError) as caught:
        model.propose_action(_context())
    assert caught.value.reason_code == "MODEL_INVALID_RESPONSE"
    assert caught.value.usage is None


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
    assert tool["tool_name"] == "read_file"
    assert "name" not in tool
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


def test_history_limit_retains_objective_and_latest_complete_tool_turn() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        max_history_chars=8000,
        transport=httpx.MockTransport(respond),
    )
    model.propose_action(_context())
    objective = _item("objective", "Repair the adder regression.", TrustClass.AUTHORIZED_HUMAN)
    old = _item("old", "OLD" * 1500, TrustClass.UNTRUSTED_CONTENT)
    model.propose_action(_context_with(objective, old))
    latest = _item("latest", "NEW" * 1500, TrustClass.UNTRUSTED_CONTENT)
    model.propose_action(_context_with(objective, latest))
    body = json.loads(captured[-1].content)
    messages = body["messages"]
    assert len(json.dumps(messages, ensure_ascii=False)) <= 8000
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "user",
        "assistant",
        "tool",
    ]
    assert "Runtime history compaction" in messages[2]["content"]
    assert "Repair the adder regression." in messages[1]["content"]
    assert "OLD" * 100 not in json.dumps(messages)
    assert "NEW" * 100 in messages[-1]["content"]
    assert "(untrusted_content)" in messages[-1]["content"]


def test_history_limit_refuses_oversized_protected_context_before_network() -> None:
    model = OllamaModel(model="m", tools=TOOLS, template=TEMPLATE, max_history_chars=4096)
    oversized = _context_with(_item("objective", "X" * 5000, TrustClass.AUTHORIZED_HUMAN))
    with pytest.raises(ModelTurnError, match="exceed history limit") as error:
        model.propose_action(oversized)
    assert error.value.reason_code == "MODEL_CONTEXT_LIMIT"


@pytest.mark.parametrize("limit", [0, 4095, True, 5000.5])
def test_invalid_history_limit_denied(limit: object) -> None:
    with pytest.raises(ValueError, match="at least 4096"):
        OllamaModel(model="m", tools=TOOLS, template=TEMPLATE, max_history_chars=limit)  # type: ignore[arg-type]


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


@pytest.mark.parametrize("count", [0, 2])
def test_rejected_batch_gets_protocol_feedback_before_valid_retry(count: int) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        calls = [
            {"function": {"name": "read_file", "arguments": {"path": "untrusted"}}}
            for _ in range(count)
        ]
        payload = _chat_payload(tool_calls=calls) if len(requests) == 1 else _chat_payload()
        return httpx.Response(200, json=payload)

    model = _model(httpx.MockTransport(respond))
    with pytest.raises(ModelTurnError, match="expected exactly one"):
        model.propose_action(_context())
    turn = model.propose_action(_context())
    assert turn.action.tool_name == "read_file"
    messages = cast(list[dict[str, object]], requests[1]["messages"])
    assert isinstance(messages[-1]["content"], str)
    assert "exactly ONE tool call" in messages[-1]["content"]
    assert "untrusted" not in messages[-1]["content"]
    assert all(message["role"] != "assistant" for message in messages)


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


@pytest.mark.parametrize(
    "case",
    [
        "parse",
        "xml_parse",
        "xml_other",
        "xml_suffix",
        "outage",
        "malformed",
        "oversized",
        "wrong_status",
    ],
)
def test_provider_tool_parser_failure_gets_safe_per_run_protocol_feedback(case: str) -> None:
    requests: list[dict[str, object]] = []
    private = "PRIVATE_PROVIDER_FRAGMENT"
    parse_error = {"error": "error parsing tool call: XML syntax error " + private}
    if case in {"xml_parse", "xml_suffix"}:
        parse_error = {
            "error": "XML syntax error on line 3: element <function> closed by </parameter>"
        }
        if case == "xml_suffix":
            parse_error["error"] += private
    elif case == "xml_other":
        parse_error = {"error": "XML syntax error on line 3: element <document> closed by </other>"}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) != 1:
            return httpx.Response(200, json=_chat_payload())
        if case == "malformed":
            return httpx.Response(500, text="tool call parsing " + private)
        if case == "oversized":
            return httpx.Response(500, json={"error": parse_error["error"] + "x" * 9000})
        return httpx.Response(
            503 if case == "wrong_status" else 500,
            json=parse_error
            if case != "outage"
            else {"error": "model allocation failed " + private},
        )

    model = OllamaModel(
        model="m", tools=TOOLS, template=TEMPLATE, transport=httpx.MockTransport(respond)
    )
    with pytest.raises(ModelTurnError) as caught:
        model.propose_action(_context())
    assert caught.value.reason_code == (
        "MODEL_TOOL_PARSE" if case in {"parse", "xml_parse"} else "MODEL_UNAVAILABLE"
    )
    assert caught.value.failure_class is ModelFailureClass.TRANSIENT
    assert caught.value.usage is None
    assert private not in caught.value.summary
    assert model.propose_action(_context()).action.tool_name == "read_file"
    second = json.dumps(requests[1])
    assert ("Protocol correction: the provider rejected" in second) is (
        case in {"parse", "xml_parse"}
    )
    assert private not in second
    assert requests[0]["tools"] == requests[1]["tools"]
    assert requests[0]["options"] == requests[1]["options"]
    current = _context()
    other = ModelContext(
        run_id=RunId("other-provider-run"),
        items=current.items,
        assembled_at=NOW,
        role=current.role,
        prompt_template=current.prompt_template,
    )
    model.propose_action(other)
    assert "Protocol correction: the provider rejected" not in json.dumps(requests[2])


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


def test_request_envelope_compacts_complete_turns_and_exposes_current_errors() -> None:
    payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert len(request.content) <= 8192 - 1024 - 1024
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        history_overflow="trim",
        context_window_tokens=8192,
        max_output_tokens=1024,
        transport=httpx.MockTransport(respond),
    )
    objective = _item(
        "objective", "Keep the authorized objective intact", TrustClass.AUTHORIZED_HUMAN
    )
    model.propose_action(_context_with(objective))
    for turn in range(30):
        model.propose_action(
            _context_with(
                objective,
                _item(
                    f"{turn}:observation",
                    f"source page {turn}: " + "水" * 250,
                    TrustClass.UNTRUSTED_CONTENT,
                ),
                _item(
                    "verifier-failure",
                    "check_upload_preservation failed; preserve object",
                    TrustClass.DETERMINISTIC_OBSERVATION,
                ),
            )
        )
    assert len(payloads) == 31
    final = json.dumps(payloads[-1], ensure_ascii=False)
    assert "Runtime history compaction" in final
    assert "Keep the authorized objective intact" in final
    assert "check_upload_preservation failed" in final
    assert "source page 29:" in final
    messages = cast(list[dict[str, object]], payloads[-1]["messages"])
    for index, message in enumerate(messages):
        if message["role"] == "tool":
            assert index > 0
            assert messages[index - 1]["role"] == "assistant"
    model.close()


@pytest.mark.parametrize("mode", ["history", "state"])
@pytest.mark.parametrize("oversized", ["objective", "latest", "tools"])
def test_compaction_refuses_to_drop_required_request_content(oversized: str, mode: str) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_chat_payload())

    tools = (
        TOOLS
        if oversized != "tools"
        else (
            ModelToolSpec(
                name="read_file",
                description="x" * 20_000,
                parameters={"type": "object", "properties": {}},
            ),
        )
    )
    model = OllamaModel(
        model="m",
        tools=tools,
        template=TEMPLATE,
        history_overflow="trim",
        conversation_mode=mode,  # type: ignore[arg-type]
        context_window_tokens=8192,
        max_output_tokens=1024,
        transport=httpx.MockTransport(respond),
    )
    objective = _item(
        "objective",
        "x" * 20_000 if oversized == "objective" else "Keep task",
        TrustClass.AUTHORIZED_HUMAN,
    )
    if oversized == "latest":
        model.propose_action(_context_with(objective))
        context = _context_with(
            objective, _item("observation", "水" * 5000, TrustClass.UNTRUSTED_CONTENT)
        )
    else:
        context = _context_with(objective)
    before = len(calls)
    with pytest.raises(ModelTurnError) as caught:
        model.propose_action(context)
    assert caught.value.reason_code == "MODEL_CONTEXT_LIMIT"
    assert len(calls) == before
    model.close()


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


def test_append_only_history_preserves_prefix_and_fails_before_network_at_limit() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        max_history_chars=8000,
        history_overflow="fail",
        context_window_tokens=32768,
        transport=httpx.MockTransport(respond),
    )
    model.propose_action(_context())
    old = _item("old", "OLD" * 1000, TrustClass.UNTRUSTED_CONTENT)
    model.propose_action(_context_with(old))
    first = json.loads(captured[0].content)
    second = json.loads(captured[1].content)
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert second["options"]["num_ctx"] == 32768
    assert "OLD" * 100 in json.dumps(second)
    with pytest.raises(ModelTurnError, match="Append-only") as error:
        model.propose_action(
            _context_with(_item("new", "NEW" * 2000, TrustClass.UNTRUSTED_CONTENT))
        )
    assert error.value.reason_code == "MODEL_CONTEXT_LIMIT"
    assert len(captured) == 2


@pytest.mark.parametrize("value", [0, 1023, True, 1024.5])
def test_invalid_explicit_context_window_denied(value: object) -> None:
    with pytest.raises(ValueError, match="at least 1024"):
        OllamaModel(model="m", tools=TOOLS, template=TEMPLATE, context_window_tokens=value)  # type: ignore[arg-type]


def test_explicit_context_window_cannot_exceed_model_capability() -> None:
    capabilities = ModelCapabilities(
        provider="ollama", model="m", supports_tool_calls=True, context_window_tokens=8192
    )
    with pytest.raises(ValueError, match="exceeds model capability"):
        OllamaModel(
            model="m",
            tools=TOOLS,
            template=TEMPLATE,
            capabilities=capabilities,
            context_window_tokens=16384,
        )
    with pytest.raises(ValueError, match="trim or fail"):
        OllamaModel(model="m", tools=TOOLS, template=TEMPLATE, history_overflow="ignore")  # type: ignore[arg-type]


def test_equal_tool_failures_with_distinct_event_ids_each_reach_provider() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_chat_payload())

    model = _model(httpx.MockTransport(handler))
    model.propose_action(_context())
    for event_id in ["first", "second"]:
        model.propose_action(
            _context_with(
                _item("objective", "Repair the adder regression.", TrustClass.AUTHORIZED_HUMAN),
                _item(
                    event_id + ":observation",
                    "Required field missing",
                    TrustClass.UNTRUSTED_CONTENT,
                ),
            )
        )
    for request in captured[1:]:
        messages = json.loads(request.content)["messages"]
        tool = next(message for message in reversed(messages) if message["role"] == "tool")
        assert "Required field missing" in tool["content"]
        assert "(untrusted_content)" in tool["content"]
    model.close()


@pytest.mark.parametrize("preserve", [True, False])
def test_thinking_retention_is_opt_in_validated_and_never_authority(preserve: bool) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        response = _chat_payload()
        message = cast("dict[str, object]", response["message"])
        message.update(content="Read the actual source", thinking="A hypothesis, not verified fact")
        response.update(eval_duration=1_000_000_000, eval_count=30, load_duration=123)
        return httpx.Response(200, json=response)

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        preserve_thinking=preserve,
        transport=httpx.MockTransport(respond),
        temperature=0.6,
        sampling_options={"presence_penalty": 0},
    )
    try:
        model.propose_action(_context())
        model.propose_action(_context())
        assistant = next(
            m
            for m in cast("list[dict[str, object]]", requests[-1]["messages"])
            if m["role"] == "assistant"
        )
        assert assistant["content"] == "Read the actual source"
        assert ("thinking" in assistant) == preserve
        assert cast("dict[str, object]", requests[-1]["options"])["temperature"] == 0.6
        assert (
            cast("dict[str, object]", model.request_tracker.observations()[-1]["metrics"])[
                "decode_tokens_per_second"
            ]
            == 30
        )
    finally:
        model.close()


def test_token_counter_sees_complete_request_and_invalid_count_denied() -> None:
    seen: list[dict[str, object]] = []

    def count(payload: dict[str, object]) -> int:
        seen.append(payload)
        return -1

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        context_window_tokens=32768,
        request_token_counter=count,
        sampling_options={"presence_penalty": 0},
        transport=_responding(_chat_payload()),
    )
    try:
        with pytest.raises(ValueError, match="invalid occupancy"):
            model.propose_action(_context())
        assert seen[0]["keep_alive"] == "5m"
        assert cast("dict[str, object]", seen[0]["options"])["presence_penalty"] == 0
    finally:
        model.close()


@pytest.mark.parametrize("thinking", ["x" * 4097, "😀" * 1025])
def test_oversized_optional_thinking_is_omitted_whole_with_notice(thinking: str) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        payload = _chat_payload()
        cast("dict[str, object]", payload["message"])["thinking"] = thinking
        return httpx.Response(200, json=payload)

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        preserve_thinking=True,
        transport=httpx.MockTransport(respond),
    )
    try:
        model.propose_action(_context())
        model.propose_action(_context())
        messages = cast("list[dict[str, object]]", requests[-1]["messages"])
        assistant = next(m for m in messages if m["role"] == "assistant")
        assert "thinking" not in assistant
        assert assistant["tool_calls"]
        assert any("omitted whole" in str(m.get("content", "")) for m in messages)
        assert messages[messages.index(assistant) + 1]["role"] == "tool"
    finally:
        model.close()


@pytest.mark.parametrize("count", [None, 17])
def test_admission_records_units_and_bounds_actual_output(count: int | None) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        context_window_tokens=32768,
        request_token_counter=(lambda _: count) if count is not None else None,
        transport=httpx.MockTransport(respond),
    )
    try:
        turn = model.propose_action(_context())
        admission = turn.context_admission
        assert admission is not None
        assert admission.admitted
        assert admission.serialized_request_bytes == len(requests[0].content)
        assert admission.input_token_bound == (
            count if count is not None else len(requests[0].content)
        )
        assert admission.measurement_basis.value == (
            "token_counter" if count is not None else "utf8_byte_upper_bound"
        )
        assert admission.output_reserve_tokens == 2048
        assert json.loads(requests[0].content)["options"]["num_predict"] == 2048
    finally:
        model.close()


def test_output_reservation_cannot_be_bypassed_by_zero_input_count() -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"inadmissible request was sent to {request.url.host}")

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        context_window_tokens=1024,
        request_token_counter=lambda _: 0,
        transport=httpx.MockTransport(unexpected),
    )
    try:
        with pytest.raises(ModelTurnError) as caught:
            model.propose_action(_context())
        admission = caught.value.context_admission
        assert admission is not None
        assert not admission.admitted
        assert admission.output_reserve_tokens == 2048
    finally:
        model.close()


def test_compaction_keeps_current_human_constraints_with_arbitrary_item_names() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        context_window_tokens=32768,
        transport=httpx.MockTransport(respond),
    )
    human = _item(
        "arbitrary-human-key", "Do not change the public API", TrustClass.AUTHORIZED_HUMAN
    )
    try:
        model.propose_action(_context())
        model.propose_action(_context_with(human))
        model.propose_action(
            _context_with(human, _item("old-source", "x" * 800, TrustClass.UNTRUSTED_CONTENT))
        )
        history = json.loads(captured[-1].content)["messages"]
        assert model._compact_history(history, _context_with(human))  # pyright: ignore[reportPrivateUsage]
        notice = next(
            message
            for message in history
            if str(message.get("content", "")).startswith("Runtime history compaction:")
        )
        assert "Do not change the public API" in notice["content"]
        assert "authorized_human" in notice["content"]
    finally:
        model.close()


def test_default_bounded_output_limit_also_governs_thinking_fallback() -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        payload = (
            _chat_payload(tool_calls=[], output_tokens=2048)
            if len(requests) == 1
            else _chat_payload()
        )
        return httpx.Response(200, json=payload)

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        thinking=True,
        context_window_tokens=32768,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(ModelTurnError, match="disable thinking"):
            model.propose_action(_context())
        model.propose_action(_context())
        assert requests[0]["think"] is True
        assert requests[1]["think"] is False
        assert requests[0]["options"] == requests[1]["options"]
    finally:
        model.close()


@pytest.mark.parametrize("content", ["x" * 4097, "😀" * 1025])
def test_large_optional_commentary_does_not_displace_exact_action(content: str) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        payload = _chat_payload()
        cast("dict[str, object]", payload["message"])["content"] = content
        return httpx.Response(200, json=payload)

    model = _model(httpx.MockTransport(respond))
    try:
        model.propose_action(_context())
        model.propose_action(_context())
        messages = cast("list[dict[str, object]]", requests[-1]["messages"])
        assistant = next(m for m in messages if m["role"] == "assistant")
        assert "omitted whole" in str(assistant["content"])
        assert assistant["tool_calls"] == [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": {"path": "adder.py"}},
            }
        ]
        assert messages[messages.index(assistant) + 1]["role"] == "tool"
    finally:
        model.close()


def test_state_requests_do_not_replay_prior_observations_or_grow() -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        conversation_mode="state",
        transport=httpx.MockTransport(respond),
    )
    for index in range(25):
        model.propose_action(
            _context_with(
                _item("objective", "Keep human authority", TrustClass.AUTHORIZED_HUMAN),
                _item("latest", f"CURRENT-{index:02d}", TrustClass.UNTRUSTED_CONTENT),
            )
        )
    sizes = [len(json.dumps(request)) for request in requests]
    assert max(sizes) == min(sizes)
    assert "CURRENT-24" in json.dumps(requests[-1])
    assert "CURRENT-00" not in json.dumps(requests[-1])
    assert "Keep human authority" in json.dumps(requests[-1])
    assert [message["role"] for message in requests[-1]["messages"]] == ["system", "user"]  # type: ignore[union-attr]
    model.close()


def test_state_protocol_correction_is_one_turn_and_run_scoped() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.content.decode())
        return httpx.Response(
            200, json=_chat_payload(tool_calls=[] if len(requests) == 1 else None)
        )

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        conversation_mode="state",
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(ModelTurnError):
        model.propose_action(_context())
    model.propose_action(replace(_context(), run_id=RunId("other-run")))
    assert "Previous response" not in requests[-1]
    model.propose_action(_context())
    assert "Previous response" in requests[-1]
    model.propose_action(_context())
    assert "Previous response" not in requests[-1]
    model.close()


def test_state_oversize_fails_without_trimming_or_transport() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        pytest.fail("Oversize current state must never reach transport")

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        conversation_mode="state",
        max_history_chars=4096,
        transport=httpx.MockTransport(reject),
    )
    with pytest.raises(ModelTurnError) as raised:
        model.propose_action(
            _context_with(
                _item("large", "X" * 10000, TrustClass.AUTHORIZED_HUMAN),
            )
        )
    assert raised.value.reason_code == "MODEL_CONTEXT_LIMIT"
    model.close()


def test_state_rejects_retained_thinking() -> None:
    with pytest.raises(ValueError, match="cannot retain"):
        OllamaModel(
            model="m",
            tools=TOOLS,
            template=TEMPLATE,
            conversation_mode="state",
            preserve_thinking=True,
        )


@pytest.mark.parametrize("rename", [False, True])
def test_schema_filter_preserves_registered_tool_identity(rename: bool) -> None:
    requests: list[bytes] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return httpx.Response(200, json=_chat_payload())

    def narrow(_context: ModelContext, spec: ModelToolSpec) -> ModelToolSpec:
        return replace(
            spec,
            name="unregistered" if rename else spec.name,
            description="Current phase description",
        )

    model = OllamaModel(
        model="test",
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(response),
        tool_spec_filter=narrow,
    )
    try:
        if rename:
            with pytest.raises(ModelTurnError, match="preserve registered tool identities"):
                model.propose_action(_context())
            assert not requests
        else:
            model.propose_action(_context())
            assert json.loads(requests[0])["tools"][0]["function"]["description"] == (
                "Current phase description"
            )
    finally:
        model.close()


def test_qualification_records_the_actual_local_request_payload() -> None:
    recorded: list[dict[str, object]] = []
    transported: list[dict[str, object]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        transported.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="test", tools=TOOLS, template=TEMPLATE, transport=httpx.MockTransport(reply)
    )
    model.request_recorder = recorded.append
    try:
        model.propose_action(_context())
        assert recorded == transported
        assert recorded[0]["tools"]
    finally:
        model.close()


def _ratio_model(transport: httpx.MockTransport, **kwargs: object) -> OllamaModel:
    return OllamaModel(
        model="devstral-small-2:latest",
        tools=TOOLS,
        template=TEMPLATE,
        transport=transport,
        context_window_tokens=8192,
        max_output_tokens=1024,
        **kwargs,  # type: ignore[arg-type]
    )


def _large_context(size: int) -> ModelContext:
    return _context_with(
        _item("objective", "Repair the adder regression.", TrustClass.AUTHORIZED_HUMAN),
        _item("source", "x" * size, TrustClass.UNTRUSTED_CONTENT),
    )


def test_byte_ratio_admits_a_request_the_strict_byte_gate_rejects() -> None:
    context = _large_context(9000)
    strict = _ratio_model(_responding(_chat_payload(prompt_tokens=2600)))
    with pytest.raises(ModelTurnError) as denied:
        strict.propose_action(context)
    assert denied.value.reason_code == "MODEL_CONTEXT_LIMIT"
    ratio = _ratio_model(_responding(_chat_payload(prompt_tokens=2600)), byte_ratio_admission=True)
    turn = ratio.propose_action(context)
    assert turn.context_admission is not None
    assert (
        turn.context_admission.measurement_basis
        is InputMeasurementBasis.UTF8_BYTES_PER_TOKEN_FLOOR_2
    )
    serialized = turn.context_admission.serialized_request_bytes
    assert turn.context_admission.input_token_bound == -(-serialized // 2)
    assert serialized > 8192


def test_ratio_violation_restores_the_strict_byte_bound_for_that_run() -> None:
    context = _large_context(3000)
    model = _ratio_model(_responding(_chat_payload(prompt_tokens=7000)), byte_ratio_admission=True)
    first = model.propose_action(context)
    assert first.context_admission is not None
    assert (
        first.context_admission.measurement_basis
        is InputMeasurementBasis.UTF8_BYTES_PER_TOKEN_FLOOR_2
    )
    second = model.propose_action(context)
    assert second.context_admission is not None
    assert second.context_admission.measurement_basis is InputMeasurementBasis.UTF8_BYTE_UPPER_BOUND
    assert "byte_ratio_bound_exceeded_strict_bound_restored" in json.dumps(
        model.request_tracker.observations()
    )


def test_two_request_measurements_cannot_be_combined() -> None:
    with pytest.raises(ValueError, match="one code-owned request measurement"):
        _ratio_model(
            _responding(_chat_payload()), byte_ratio_admission=True, request_token_counter=len
        )


def test_evidence_driven_exhaustion_skips_reasoning_once_then_resumes() -> None:
    requests: list[dict[str, object]] = []
    run_tests: list[object] = [{"function": {"name": "run_tests", "arguments": {}}, "id": "call_t"}]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:  # verbose thinking used the whole allowance, no action
            return httpx.Response(200, json=_chat_payload(tool_calls=[], output_tokens=64))
        return httpx.Response(200, json=_chat_payload(tool_calls=run_tests))

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        thinking=True,
        thinking_policy="on_evidence",
        max_output_tokens=64,
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(ModelTurnError) as exc:
        model.propose_action(_context())
    assert "the next call disables thinking" in exc.value.summary
    model.propose_action(_context())  # forced action without reasoning
    model.propose_action(_context())  # new evidence from run_tests: reasoning resumes
    assert [r["think"] for r in requests] == [True, False, True]


def _history_after_long_thinking(replay: str) -> list[dict[str, object]]:
    requests: list[dict[str, object]] = []
    long_thinking = "step " * 2000  # > 4096 bytes
    payload = _chat_payload()
    payload["message"]["thinking"] = long_thinking  # type: ignore[index]
    payload["message"]["content"] = "plan " * 1000  # type: ignore[index]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        thinking=True,
        preserve_thinking=True,
        history_replay=replay,  # type: ignore[arg-type]
        transport=httpx.MockTransport(respond),
    )
    model.propose_action(_context())
    model.propose_action(_context())
    return cast("list[dict[str, object]]", requests[1]["messages"])


def test_exact_replay_keeps_long_thinking_and_content_verbatim_without_notices() -> None:
    messages = _history_after_long_thinking("exact")
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["thinking"] == "step " * 2000
    assert assistant["content"] == "plan " * 1000
    assert not any("omitted" in str(m.get("content", "")) for m in messages)


def test_bounded_replay_still_omits_long_thinking_and_commentary() -> None:
    messages = _history_after_long_thinking("bounded")
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert "thinking" not in assistant
    assert "omitted whole" in str(assistant["content"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"history_replay": "exact", "preserve_thinking": False},
        {"history_replay": "exact", "preserve_thinking": True, "conversation_mode": "state"},
        {"history_replay": "verbatim"},
    ],
)
def test_exact_replay_requires_append_only_history_with_thinking(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"exact history replay|state mode|thinking"):
        OllamaModel(model="m", tools=TOOLS, template=TEMPLATE, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(("replay", "folded"), [("exact", True), ("bounded", False)])
def test_runtime_updates_ride_inside_the_tool_response_only_in_exact_mode(
    replay: str, folded: bool
) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_payload())

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        thinking=True,
        preserve_thinking=True,
        history_replay=replay,  # type: ignore[arg-type]
        transport=httpx.MockTransport(respond),
    )
    model.propose_action(_context())
    model.propose_action(
        _context_with(
            _item("objective", "Repair the adder regression.", TrustClass.AUTHORIZED_HUMAN),
            _item("obs", "Source 'adder.py': return a-b", TrustClass.UNTRUSTED_CONTENT),
            _item("plan", "Verification recorded; continue.", TrustClass.RUNTIME_POLICY),
        )
    )
    tail = cast("list[dict[str, object]]", requests[1]["messages"])[2:]
    roles = [m["role"] for m in tail]
    tool = next(m for m in tail if m["role"] == "tool")
    if folded:
        assert roles == ["assistant", "tool"]
        assert "Runtime update:" in str(tool["content"])
        assert "(runtime_policy) Verification recorded" in str(tool["content"])
    else:
        assert roles == ["assistant", "tool", "user"]
        assert "Runtime update" not in str(tool["content"])


@pytest.mark.parametrize(("replay", "role"), [("exact", "system"), ("bounded", "user")])
def test_protocol_correction_does_not_become_a_user_query_in_exact_mode(
    replay: str, role: str
) -> None:
    requests: list[dict[str, object]] = []
    two_calls = [
        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
        {"function": {"name": "read_file", "arguments": {"path": "b.py"}}},
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        calls = two_calls if len(requests) == 1 else None
        return httpx.Response(200, json=_chat_payload(tool_calls=calls))

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        thinking=True,
        preserve_thinking=True,
        history_replay=replay,  # type: ignore[arg-type]
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(ModelTurnError):
        model.propose_action(_context())
    model.propose_action(_context())
    correction = cast("list[dict[str, object]]", requests[1]["messages"])[-1]
    assert correction["role"] == role
    assert "Protocol correction" in str(correction["content"])


def _calibrated_model(replies: list[int]) -> tuple[OllamaModel, list[dict[str, object]]]:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_payload(prompt_tokens=replies[len(requests) - 1]))

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(respond),
        context_window_tokens=8192,
        max_output_tokens=1024,
        byte_ratio_admission=True,
    )
    return model, requests


def _growing(size: int) -> ModelContext:
    return _context_with(
        _item("objective", "Repair the adder regression.", TrustClass.AUTHORIZED_HUMAN),
        _item(f"obs{size}", "x" * size, TrustClass.UNTRUSTED_CONTENT),
    )


def test_provider_count_calibrates_the_next_bound_instead_of_compacting() -> None:
    model, _ = _calibrated_model([1900, 2900])
    first = model.propose_action(_growing(8000))
    assert first.context_admission is not None
    assert (
        first.context_admission.measurement_basis
        is InputMeasurementBasis.UTF8_BYTES_PER_TOKEN_FLOOR_2
    )
    second = model.propose_action(_growing(3500))
    admission = second.context_admission
    assert admission is not None
    assert admission.measurement_basis is InputMeasurementBasis.PROVIDER_COUNT_PLUS_DELTA
    strict_ratio = -(-admission.serialized_request_bytes // 2)
    assert strict_ratio + 1024 + 1024 > 8192  # the old ratio bound would have compacted
    assert admission.input_token_bound < strict_ratio
    assert admission.compacted_turns == 0


def test_provider_count_above_the_bound_fails_closed_to_the_strict_bound() -> None:
    model, _ = _calibrated_model([9000, 100])
    model.propose_action(_growing(2000))
    second = model.propose_action(_growing(100))
    assert second.context_admission is not None
    assert second.context_admission.measurement_basis is InputMeasurementBasis.UTF8_BYTE_UPPER_BOUND


def test_compaction_frees_headroom_instead_of_trimming_one_turn_per_request() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        # Realistic provider count: ~3.4 bytes per token of the actual request.
        tokens = int(len(request.content) / 3.4)
        return httpx.Response(200, json=_chat_payload(prompt_tokens=tokens))

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(respond),
        context_window_tokens=8192,
        max_output_tokens=1024,
        byte_ratio_admission=True,
    )
    turns = [model.propose_action(_growing(2800 + i)) for i in range(10)]
    compacted = [t for t in turns if t.context_admission and t.context_admission.compacted_turns]
    assert compacted, "test must exercise compaction"
    budget = 8192 - 1024 - 1024
    first = compacted[0].context_admission
    assert first is not None
    assert first.input_token_bound <= budget * 3 // 4
    # Headroom means the next turns grow without another compaction right away.
    index = turns.index(compacted[0])
    following = [t.context_admission for t in turns[index + 1 : index + 3]]
    assert all(a is not None and a.compacted_turns == first.compacted_turns for a in following)
