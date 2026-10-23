"""Offline bridge regressions; no model server or network calls."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from loopforge.adapters.ollama_model import ChatResponse
from scripts.replay_successful_repair import assert_context_parity
from scripts.run_mlx_comparison import mlx_request, native_response


def packet(messages: list[dict[str, object]]) -> dict[str, object]:
    return {
        "messages": messages,
        "tools": [],
        "think": True,
        "options": {"num_predict": 4096, "temperature": 1.0, "presence_penalty": 1.5},
    }


def test_native_tool_history_retains_exact_arguments_and_association() -> None:
    data = packet(
        [
            {"role": "user", "content": "Inspect source"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "app/a.py", "start_line": "1"},
                        }
                    }
                ],
            },
            {"role": "tool", "tool_name": "read_file", "content": "UNTRUSTED: source bytes"},
        ]
    )
    original = json.dumps(data)
    mapped = json.loads(json.dumps(mlx_request(data, "pinned-local-model")))
    assert json.dumps(data) == original
    call = mapped["messages"][1]["tool_calls"][0]
    result = mapped["messages"][2]
    assert result["tool_call_id"] == call["id"]
    assert result["content"] == "UNTRUSTED: source bytes"
    assert json.loads(call["function"]["arguments"]) == {"path": "app/a.py", "start_line": "1"}
    assert mapped["chat_template_kwargs"] == {"enable_thinking": True}
    assert mapped["max_tokens"] == 4096


def test_orphan_tool_results_fail_before_provider_request() -> None:
    with pytest.raises(ValueError, match="orphan"):
        mlx_request(packet([{"role": "tool", "tool_name": "read_file", "content": "x"}]), "model")


@pytest.mark.parametrize(
    ("arguments", "accepted"), [('{"path":"app/a.py"}', True), ('{"path":123}', False)]
)
def test_bridge_preserves_strict_argument_boundary(arguments: str, accepted: bool) -> None:
    data: dict[str, object] = {
        "model": "mlx-model",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"function": {"name": "read_file", "arguments": arguments}}],
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }
    native = native_response(data)
    assert "eval_duration" not in native
    if accepted:
        calls = ChatResponse.model_validate(native).message.tool_calls
        assert calls is not None
        assert calls[0].function.arguments == {"path": "app/a.py"}
    else:
        with pytest.raises(ValidationError):
            ChatResponse.model_validate(native)


def test_length_stop_is_not_hidden_as_success() -> None:
    data: dict[str, object] = {
        "model": "mlx",
        "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 4096},
    }
    assert native_response(data)["done_reason"] == "length"


def test_archived_context_content_cannot_be_dropped_or_rewritten() -> None:

    original = packet(
        [
            {"role": "user", "content": "Original objective and source evidence"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "app/tests/test_api.py"},
                        }
                    }
                ],
            },
            {"role": "tool", "tool_name": "read_file", "content": "Measured source and failure"},
        ]
    )
    mapped = json.loads(json.dumps(mlx_request(original, "pinned-model")))
    assert_context_parity(original, mapped)
    truncated = deepcopy(mapped)
    truncated["messages"].pop()
    with pytest.raises(AssertionError, match="Message count"):
        assert_context_parity(original, truncated)
    rewritten = deepcopy(mapped)
    rewritten["messages"][-1]["content"] = "Generic summary instead of evidence"
    with pytest.raises(AssertionError, match="Message content"):
        assert_context_parity(original, rewritten)


def test_upstream_mlx_reasoning_field_is_preserved_without_fabricated_metrics() -> None:
    response: dict[str, object] = {
        "model": "mlx",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "", "reasoning": "fixture reasoning", "tool_calls": []},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    normalized = native_response(response)
    assert ChatResponse.model_validate(normalized).message.thinking == "fixture reasoning"
    assert "eval_duration" not in normalized
