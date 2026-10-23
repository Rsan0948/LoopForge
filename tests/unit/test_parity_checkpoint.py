"""Conversation parity and continuation use the existing adapter; no network."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from loopforge.domain.security import TrustClass
from loopforge.ports.model import ModelTurnError
from scripts.parity_checkpoint import CheckpointOllamaModel
from scripts.replay_successful_repair import read_verified_capture
from tests.unit.test_ollama_model import (
    TEMPLATE,
    TOOLS,
    _chat_payload,  # pyright: ignore[reportPrivateUsage] - existing shared test fixtures
    _context,  # pyright: ignore[reportPrivateUsage] - existing shared test fixtures
    _context_with,  # pyright: ignore[reportPrivateUsage] - existing shared test fixtures
    _item,  # pyright: ignore[reportPrivateUsage] - existing shared test fixtures
)


def test_restored_history_is_exact_then_native_tool_feedback_appends() -> None:
    calls: list[dict[str, object]] = []
    history = [
        {"role": "system", "content": "Original system instructions"},
        {"role": "user", "content": "Original goal and full supervisor evidence"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "app/test.py"}}}
            ],
        },
        {"role": "tool", "tool_name": "read_file", "content": "Original source and failure"},
    ]
    checkpoint: dict[str, object] = {"model": "test", "messages": history}

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_payload())

    model = CheckpointOllamaModel(
        model="test",
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(respond),
        thinking=True,
        thinking_policy="on_evidence",
        max_output_tokens=32768,
        context_window_tokens=131072,
    )
    try:
        model.restore_checkpoint(checkpoint)
        model.propose_action(_context())
        assert calls[0]["messages"] == history
        assert calls[0]["think"] is False
        assert json.loads(json.dumps(calls[0]["options"]))["num_predict"] == 32768
        model.propose_action(
            _context_with(_item("fresh", "Current measured failure", TrustClass.UNTRUSTED_CONTENT))
        )
        messages = json.loads(json.dumps(calls[1]["messages"]))
        assert messages[: len(history)] == history
        assert "Current measured failure" in messages[-1]["content"]
        assert messages[-1]["role"] == "tool"
        with pytest.raises(ValueError, match="active conversation"):
            model.restore_checkpoint(checkpoint)
    finally:
        model.close()


@pytest.mark.parametrize(
    ("name", "last"), [("wrong-model", "read_file"), ("test", "invented_tool")]
)
def test_checkpoint_cannot_change_model_or_end_at_an_unregistered_tool(
    name: str, last: str
) -> None:
    model = CheckpointOllamaModel(
        model="test",
        tools=TOOLS,
        template=TEMPLATE,
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    try:
        with pytest.raises(ValueError, match="Checkpoint"):
            model.restore_checkpoint(
                {
                    "model": name,
                    "messages": [
                        {"role": "system", "content": "original"},
                        {"role": "tool", "tool_name": last, "content": "result"},
                    ],
                }
            )
    finally:
        model.close()


@pytest.mark.parametrize("count", [0, 2])
def test_invalid_first_checkpoint_response_retries_without_orphan_tool_result(count: int) -> None:
    history = [
        {"role": "system", "content": "original"},
        {"role": "user", "content": "original goal"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "app/test.py"}}}
            ],
        },
        {"role": "tool", "tool_name": "read_file", "content": "original source"},
    ]
    bodies: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        payload = _chat_payload()
        if len(bodies) == 1:
            payload = _chat_payload(
                tool_calls=[
                    {"function": {"name": "read_file", "arguments": {"path": "app/test.py"}}}
                ]
                * count
            )
        return httpx.Response(200, json=payload)

    model = CheckpointOllamaModel(
        model="test",
        tools=TOOLS,
        template=TEMPLATE,
        thinking=True,
        thinking_policy="on_evidence",
        transport=httpx.MockTransport(respond),
    )
    try:
        model.restore_checkpoint({"model": "test", "messages": history})
        with pytest.raises(ModelTurnError):
            model.propose_action(_context())
        model.propose_action(_context())
        messages = json.loads(json.dumps(bodies[1]["messages"]))
        assert messages[: len(history)] == history
        assert all(m["role"] != "tool" for m in messages[len(history) :])
        assert messages[-1]["role"] == "user"
    finally:
        model.close()


def test_captured_context_bytes_must_match_the_pinned_successful_request(tmp_path: Path) -> None:
    data = {"messages": [{"role": "user", "content": "Preserved successful handoff"}]}
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(data))
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_verified_capture(path, expected) == data
    path.write_text(json.dumps({"messages": [{"role": "user", "content": "Generic replacement"}]}))
    with pytest.raises(ValueError, match="context substitution"):
        read_verified_capture(path, expected)
