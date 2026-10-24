"""The MLX engine behind OllamaModel's native protocol; no network, no MLX install."""

import inspect
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import httpx
import pytest

from loopforge.adapters import mlx_transport
from loopforge.adapters.mlx_transport import (
    PRESENCE_CONTEXT_TOKENS,
    MlxChatTransport,
    lab_transport,
    mlx_request,
)
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.entrypoints import goal_runner
from loopforge.entrypoints.campaign_models import CampaignConfig
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.ports.model import ModelTurnError
from tests.unit.test_ollama_model import (
    TEMPLATE,
    TOOLS,
    _context,  # pyright: ignore[reportPrivateUsage] - shared fixtures
)

OPTIONS = {
    "num_predict": 16384,
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
}


def _completion(**message: object) -> dict[str, object]:
    return {
        "model": "/models/qwen",
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {
            "prompt_tokens": 900,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 850},
        },
    }


READ = _completion(
    content="Plan: read the adder.",
    reasoning_content="The failure points at adder.py.",
    tool_calls=[{"function": {"name": "read_file", "arguments": '{"path": "adder.py"}'}}],
)


def _transport(
    reply: Mapping[str, object] | None = None, status: int = 200
) -> tuple[MlxChatTransport, list[dict[str, object]], list[Mapping[str, object]]]:
    sent: list[dict[str, object]] = []
    rendered: list[Mapping[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        sent.append(json.loads(request.content))
        return httpx.Response(status, json=dict(reply or READ))

    def render(native: Mapping[str, object]) -> str:
        rendered.append(native)
        return "<|im_start|>rendered by ollama"

    transport = MlxChatTransport(
        model_path="/models/qwen", render=render, transport=httpx.MockTransport(respond)
    )
    return transport, sent, rendered


def test_request_carries_ollamas_prompt_sampling_and_paired_tool_ids() -> None:
    native = {
        "messages": [
            {"role": "user", "content": "Fix it."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
                    {"function": {"name": "read_file", "arguments": {"path": "b.py"}}},
                ],
            },
            {"role": "tool", "tool_name": "read_file", "content": "a"},
            {"role": "tool", "tool_name": "read_file", "content": "b"},
        ],
        "tools": [{"type": "function"}],
        "options": OPTIONS,
    }
    payload = mlx_request(native, model_path="/m", rendered_prompt="PROMPT")
    messages = cast("list[dict[str, object]]", payload["messages"])
    calls = cast("list[dict[str, object]]", messages[1]["tool_calls"])
    assert [c["id"] for c in calls] == ["call_1_0", "call_1_1"]
    assert cast("dict[str, str]", calls[0]["function"])["arguments"] == '{"path": "a.py"}'
    assert [m["tool_call_id"] for m in messages[2:]] == ["call_1_0", "call_1_1"]
    assert payload["chat_template_kwargs"] == {"ollama_rendered_prompt": "PROMPT"}
    assert payload["max_tokens"] == 16384
    assert payload["presence_penalty"] == 1.5
    assert payload["presence_context_size"] == PRESENCE_CONTEXT_TOKENS
    assert payload["stream"] is False


def test_ollama_model_runs_unchanged_on_the_mlx_engine() -> None:
    transport, sent, rendered = _transport()
    model = OllamaModel(
        model="qwen3.6:35b-a3b",
        tools=TOOLS,
        template=TEMPLATE,
        thinking=True,
        preserve_thinking=True,
        history_replay="exact",
        observation_format="delimited",
        context_window_tokens=65536,
        max_output_tokens=4096,
        transport=transport,
    )
    turn = model.propose_action(_context())
    assert turn.action.tool_name == "read_file"
    assert turn.action.arguments == {"path": "adder.py"}
    # The renderer saw the exact native request OllamaModel built.
    assert rendered[0]["model"] == "qwen3.6:35b-a3b"
    assert sent[0]["model"] == "/models/qwen"
    # Exact replay keeps the generated thinking for the next request's prefix.
    model.propose_action(_context())
    replayed = cast("list[dict[str, object]]", rendered[1]["messages"])
    assistant = next(m for m in replayed if m["role"] == "assistant")
    assert assistant["thinking"] == "The failure points at adder.py."


def test_completion_normalizes_to_the_native_shape_without_invented_timings() -> None:
    transport, _, _ = _transport()
    response = transport.handle_request(
        httpx.Request("POST", "http://mlx/api/chat", json={"messages": [], "options": OPTIONS})
    )
    native = response.json()
    assert native["message"]["thinking"] == "The failure points at adder.py."
    assert native["message"]["tool_calls"][0]["function"]["arguments"] == {"path": "adder.py"}
    assert native["prompt_eval_count"] == 900
    assert native["prompt_eval_cached_count"] == 850
    assert "prompt_eval_duration" not in native
    assert native["done_reason"] == "stop"


@pytest.mark.parametrize(
    "reply",
    [
        {"model": "m", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        {"model": "m", "choices": [{"message": {"content": 7}}], "usage": {}},
        {"unexpected": True},
    ],
    ids=["no-choice", "wrong-types", "not-a-completion"],
)
def test_invalid_completion_is_a_model_failure_not_an_action(reply: dict[str, object]) -> None:
    transport, _, _ = _transport(reply)
    model = OllamaModel(model="m", tools=TOOLS, template=TEMPLATE, transport=transport)
    with pytest.raises(ModelTurnError):
        model.propose_action(_context())


def test_malformed_tool_arguments_are_rejected_by_the_native_schema() -> None:
    transport, _, _ = _transport(
        _completion(tool_calls=[{"function": {"name": "read_file", "arguments": "{not json"}}])
    )
    response = transport.handle_request(
        httpx.Request("POST", "http://mlx/api/chat", json={"messages": [], "options": OPTIONS})
    )
    assert response.json()["message"]["tool_calls"][0]["function"]["arguments"] == "{not json"


def test_server_errors_pass_through_and_other_paths_are_not_served() -> None:
    transport, _, _ = _transport({"error": "busy"}, status=503)
    chat = httpx.Request("POST", "http://mlx/api/chat", json={"messages": [], "options": OPTIONS})
    assert transport.handle_request(chat).status_code == 503
    assert transport.handle_request(httpx.Request("GET", "http://mlx/api/tags")).status_code == 404


@pytest.mark.parametrize(
    "url", ["http://10.0.0.5:18181", "https://127.0.0.1:18181", "http://example.com"]
)
def test_only_a_loopback_http_server_is_accepted(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        MlxChatTransport(model_path="/m", render=lambda _: "p", base_url=url)


def test_lab_install_requires_the_pinned_revision_and_renderer(tmp_path: Path) -> None:
    (tmp_path / "model.json").write_text(json.dumps({"revision": "0" * 40, "local_path": "/m"}))
    with pytest.raises(ValueError, match="revision"):
        lab_transport(tmp_path)
    (tmp_path / "model.json").write_text(
        json.dumps({"revision": mlx_transport.MLX_MODEL_REVISION, "local_path": "/m"})
    )
    with pytest.raises(ValueError, match="renderer binary is missing"):
        lab_transport(tmp_path)
    binary = tmp_path / "renderer-oracle/renderer-oracle"
    binary.parent.mkdir()
    binary.write_text('#!/bin/sh\necho \'{"prompt": "rendered"}\'\n')
    binary.chmod(0o755)
    transport = lab_transport(tmp_path)
    assert transport.render({"messages": []}) == "rendered"
    transport.close()


def _campaign(**update: object) -> dict[str, object]:
    return {
        "repository": "/repo",
        "project": "reconciliation",
        "revision": "a" * 40,
        "scope": ["app"],
        "model": "qwen3.6:35b-a3b",
        "inference_profile": "qwen-native-v1",
        **update,
    }


def test_mlx_engine_is_opt_in_and_keeps_existing_intake_hashes() -> None:
    default = CampaignConfig.model_validate(_campaign())
    assert default.engine == "ollama"
    assert "engine" not in default.model_dump()
    mlx = CampaignConfig.model_validate(_campaign(engine="mlx"))
    assert mlx.model_dump()["engine"] == "mlx"


@pytest.mark.parametrize(
    "update",
    [
        {"engine": "mlx", "inference_profile": "qwen-session-v1"},
        {"engine": "mlx", "model": "qwen3.8:27b-q4_K_M"},
        {"engine": "vllm"},
    ],
    ids=["other-profile", "other-model", "unknown-engine"],
)
def test_mlx_engine_refuses_unsupported_configurations(update: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="engine"):
        CampaignConfig.model_validate(_campaign(**update))


def _intake(directory: Path, engine: str) -> None:
    from tests.unit.test_audit_core_regressions import (  # noqa: PLC0415 - shared fixture
        repository_config as config_fixture,
    )

    build = cast("Callable[[Path], RepositoryGoalConfig]", inspect.unwrap(config_fixture))
    config = build(directory)
    config = config.model_copy(
        update={
            "campaign": config.campaign.model_copy(
                update={
                    "engine": engine,
                    "model": "qwen3.6:35b-a3b",
                    "inference_profile": "qwen-native-v1",
                }
            ),
            "remote_data": "none",
        }
    )
    (directory / "repository-intake.json").write_text(config.model_dump_json())


def test_mlx_goal_checks_the_mlx_server_not_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _intake(tmp_path, "mlx")

    def no_preflight(_task: object) -> None:
        return None

    monkeypatch.setattr(goal_runner, "preflight", no_preflight)

    def no_ollama(*_args: object, **_kwargs: object) -> None:
        msg = "Ollama must not be probed for an MLX goal"
        raise AssertionError(msg)

    monkeypatch.setattr(goal_runner.httpx, "Client", no_ollama)
    monkeypatch.setattr(goal_runner, "mlx_ready", lambda: False)
    with pytest.raises(ValueError, match="MLX server is unavailable"):
        goal_runner.dependencies(tmp_path)
    opened: list[bool] = []

    class Lab:
        def close(self) -> None:
            opened.append(True)

    monkeypatch.setattr(goal_runner, "mlx_ready", lambda: True)
    monkeypatch.setattr(goal_runner, "lab_transport", Lab)
    goal_runner.dependencies(tmp_path)
    assert opened == [True]
