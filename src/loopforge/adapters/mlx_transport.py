"""Serve OllamaModel's native chat protocol from the local MLX server.

``OllamaModel`` builds the native ``/api/chat`` request (history, exact replay, sampling,
admission) and validates the native response strictly. This transport is the engine
switch beneath it: it renders the prompt with Ollama's own renderer, so both engines see
the identical prompt and the prefix cache stays exact, and sends it to the pinned
``mlx_lm`` server (``scripts/mlx_server_agentic.py``) as an OpenAI-style chat request.
The reply is normalized back to the native shape without invented timings.

Everything here is code-owned: the loopback endpoint, the pinned model revision and the
renderer binary. Model or repository content cannot change them.
"""

import json
import subprocess
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Final, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

MLX_BASE_URL: Final = "http://127.0.0.1:18181"
MLX_MODEL_REVISION: Final = "38740b847e4cb78f352aba30aa41c76e08e6eb46"
# Tuned in the lab (scripts/tune_engine_sampling.py): a 2048-token presence window gave
# shorter, far less variable reasoning on MLX with exact edit arguments intact.
PRESENCE_CONTEXT_TOKENS: Final = 2048
_RENDER_TIMEOUT_SECONDS: Final = 30

Renderer = Callable[[Mapping[str, object]], str]
_SAMPLING: Final = {
    "num_predict": "max_tokens",
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "presence_penalty": "presence_penalty",
    "repeat_penalty": "repetition_penalty",
}


class _Rendered(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prompt: StrictStr = Field(min_length=1)


def oracle_renderer(binary: Path) -> Renderer:
    """Ollama's own prompt renderer, built from ``scripts/ollama_renderer``."""
    if not binary.is_file():
        msg = f"Ollama renderer binary is missing: {binary}"
        raise ValueError(msg)

    def render(native: Mapping[str, object]) -> str:
        out = subprocess.run(  # A fixed code-owned binary; the request is JSON on stdin.
            [str(binary)],
            input=json.dumps(native),
            capture_output=True,
            text=True,
            check=True,
            timeout=_RENDER_TIMEOUT_SECONDS,
        )
        return _Rendered.model_validate_json(out.stdout).prompt

    return render


def _message(index: int, original: Mapping[str, object], pending: list[str]) -> dict[str, object]:
    message: dict[str, object] = {
        "role": original["role"],
        "content": original.get("content", ""),
    }
    calls = original.get("tool_calls")
    if original["role"] == "assistant" and isinstance(calls, list):
        pending.clear()
        converted: list[dict[str, object]] = []
        for j, call in enumerate(cast("list[Mapping[str, Mapping[str, object]]]", calls)):
            function = call["function"]
            pending.append(f"call_{index}_{j}")
            converted.append(
                {
                    "id": pending[-1],
                    "type": "function",
                    "function": {
                        "name": function["name"],
                        "arguments": json.dumps(function.get("arguments", {})),
                    },
                }
            )
        message["tool_calls"] = converted
    if original["role"] == "tool":
        # Native tool results pair with the preceding calls positionally.
        message["name"] = original.get("tool_name", original.get("name"))
        message["tool_call_id"] = pending.pop(0) if pending else f"orphan_{index}"
    return message


def mlx_request(
    native: Mapping[str, object], *, model_path: str, rendered_prompt: str
) -> dict[str, object]:
    """The native request as an OpenAI-shaped chat request carrying Ollama's prompt.

    The server's chat template (``scripts/ollama_renderer/chat_template.jinja``) emits
    ``ollama_rendered_prompt`` verbatim; the framed messages drive only its tool-result
    cache checkpoint and tool-call parsing.
    """
    pending: list[str] = []
    raw_messages = cast("list[Mapping[str, object]]", native.get("messages", []))
    options = cast("Mapping[str, object]", native["options"])
    # Sampling the native request sets is forwarded; unset values keep the server default.
    sampling: dict[str, object] = {
        mlx: options[native_name]
        for native_name, mlx in _SAMPLING.items()
        if native_name in options
    }
    return {
        "model": model_path,
        "messages": [_message(i, m, pending) for i, m in enumerate(raw_messages)],
        "tools": native.get("tools", []),
        "stream": False,
        **sampling,
        "presence_context_size": PRESENCE_CONTEXT_TOKENS,
        "chat_template_kwargs": {"ollama_rendered_prompt": rendered_prompt},
    }


class _Function(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    name: StrictStr
    arguments: StrictStr | dict[StrictStr, object] = "{}"


class _Call(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    function: _Function


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    content: StrictStr | None = None
    reasoning_content: StrictStr | None = None
    reasoning: StrictStr | None = None
    tool_calls: list[_Call] | None = None


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    message: _Message
    finish_reason: StrictStr | None = None


class _CachedDetails(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    cached_tokens: int = Field(default=0, ge=0)


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    prompt_tokens_details: _CachedDetails | None = None


class _Completion(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    model: StrictStr
    choices: list[_Choice] = Field(min_length=1, max_length=1)
    usage: _Usage


def native_response(completion: _Completion) -> dict[str, object]:
    """The MLX completion in the native response shape OllamaModel validates."""
    choice = completion.choices[0]
    message = choice.message
    calls: list[dict[str, object]] = []
    for call in message.tool_calls or []:
        arguments = call.function.arguments
        if isinstance(arguments, str):
            # Malformed JSON stays a string; OllamaModel's strict schema rejects the
            # call and the model gets the normal invalid-call feedback.
            with suppress(json.JSONDecodeError):
                arguments = cast("dict[str, object]", json.loads(arguments))
        calls.append({"function": {"name": call.function.name, "arguments": arguments}})
    details = completion.usage.prompt_tokens_details
    return {
        "model": completion.model,
        "done": True,
        "done_reason": "length" if choice.finish_reason == "length" else "stop",
        "message": {
            "role": "assistant",
            "content": message.content or "",
            "thinking": message.reasoning_content or message.reasoning or "",
            "tool_calls": calls,
        },
        "prompt_eval_count": completion.usage.prompt_tokens,
        "eval_count": completion.usage.completion_tokens,
        "prompt_eval_cached_count": details.cached_tokens if details else 0,
    }


class MlxChatTransport(httpx.BaseTransport):
    """An httpx transport answering ``POST /api/chat`` from the MLX server."""

    def __init__(
        self,
        *,
        model_path: str,
        render: Renderer,
        base_url: str = MLX_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme != "http" or url.host not in {"127.0.0.1", "localhost"}:
            msg = "The MLX server must be a loopback HTTP endpoint"
            raise ValueError(msg)
        self.model_path, self.render = model_path, render
        self.client = httpx.Client(base_url=base_url, transport=transport, trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/api/chat":
            return httpx.Response(404, json={"error": "only /api/chat is served"})
        native = cast("dict[str, object]", json.loads(request.content))
        payload = mlx_request(
            native, model_path=self.model_path, rendered_prompt=self.render(native)
        )
        # OllamaModel's per-request timeout carries over to the MLX request.
        timeout = cast("dict[str, float | None] | None", request.extensions.get("timeout"))
        response = self.client.post(
            "/v1/chat/completions",
            json=payload,
            timeout=httpx.Timeout(**timeout) if timeout else httpx.USE_CLIENT_DEFAULT,
        )
        if not response.is_success:
            return httpx.Response(response.status_code, content=response.content)
        try:
            completion = _Completion.model_validate_json(response.content)
        except ValidationError:
            return httpx.Response(502, json={"error": "invalid MLX completion"})
        return httpx.Response(200, json=native_response(completion))

    def close(self) -> None:
        self.client.close()


class _LabModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    revision: StrictStr
    local_path: StrictStr


def lab_transport(lab: Path) -> MlxChatTransport:
    """The transport for the pinned lab install (model manifest and built renderer)."""
    model = _LabModel.model_validate_json((lab / "model.json").read_text())
    if model.revision != MLX_MODEL_REVISION:
        msg = "Unexpected MLX model revision"
        raise ValueError(msg)
    return MlxChatTransport(
        model_path=model.local_path,
        render=oracle_renderer(lab / "renderer-oracle/renderer-oracle"),
    )


def mlx_ready(base_url: str = MLX_BASE_URL, *, timeout: float = 5) -> bool:
    """Whether the MLX server answers on its model listing."""
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            return client.get(f"{base_url}/v1/models").is_success
    except httpx.HTTPError:
        return False
