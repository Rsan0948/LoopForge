"""qwen-native-v1: append-only native tool chat, reasoning on, conservative byte-ratio admission.

Driven through the real coding composition with a hermetic Ollama transport; no network.
"""

import json
from itertools import pairwise
from pathlib import Path
from typing import cast

import httpx
import pytest

from loopforge.adapters import ollama_model
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.domain.types import RunStatus
from loopforge.entrypoints import campaign_coding as coding
from tests.unit.test_campaign_coding import FakeChecks, campaign_config, contract, workspace


def _messages(payload: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", payload["messages"])


def _reply(tool: str, arguments: dict[str, str]) -> dict[str, object]:
    return {
        "model": "qwen",
        "done": True,
        "prompt_eval_count": 900,
        "eval_count": 300,
        "message": {
            "role": "assistant",
            "content": f"Plan: {tool} next; then fix add() and run the check.",
            "thinking": "The failing check names src/add.py; read it, then fix the sum.",
            "tool_calls": [{"function": {"name": tool, "arguments": arguments}}],
        },
    }


@pytest.fixture
def native_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[dict[str, object]], RunStatus]:
    monkeypatch.setattr(coding, "ContainerSandbox", FakeChecks)
    repo = workspace(tmp_path)
    source = (repo.root / "src/add.py").read_text()
    script = [
        _reply("read_file", {"path": "src/add.py"}),
        _reply("read_file", {"path": "docs/api.md"}),
        _reply("write_file", {"path": "src/add.py", "content": "def add(a,b): return a+b\n"}),
        _reply("check_regression", {}),
    ]
    payloads: list[dict[str, object]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=script[len(payloads) - 1])

    real = ollama_model.OllamaModel

    def factory(**kwargs: object) -> ollama_model.OllamaModel:
        return real(**{**kwargs, "transport": httpx.MockTransport(transport)})  # type: ignore[arg-type]

    monkeypatch.setattr(coding, "OllamaModel", factory)
    config = campaign_config(repo).model_copy(
        update={"inference_profile": "qwen-native-v1", "model": "qwen3.8:27b-q4_K_M"}
    )
    runtime, _ = coding.build_coding(config, contract(), repo, store=InMemoryEventStore())
    result = runtime.run("Fix addition")
    assert source != (repo.root / "src/add.py").read_text()
    return payloads, result.status


def test_native_requests_enable_reasoning_and_recommended_sampling(
    native_run: tuple[list[dict[str, object]], RunStatus],
) -> None:
    payloads, status = native_run
    assert status is RunStatus.SUCCEEDED
    first = payloads[0]
    assert first["think"] is True
    options = first["options"]
    assert isinstance(options, dict)
    assert options["temperature"] == 0.6
    assert options["num_predict"] == 8192
    assert options["num_ctx"] == 98304
    assert options["top_k"] == 20


def test_native_conversation_is_append_only_so_the_prefix_cache_is_reused(
    native_run: tuple[list[dict[str, object]], RunStatus],
) -> None:
    payloads, _ = native_run
    assert len(payloads) == 3
    for earlier, later in pairwise(payloads):
        before, after = _messages(earlier), _messages(later)
        assert after[: len(before)] == before, "a turn rewrote earlier messages"
    last = _messages(payloads[-1])
    roles = [m["role"] for m in last]
    assert roles[:2] == ["system", "user"]
    assert roles.count("tool") == 2
    tool = next(m for m in last if m["role"] == "tool")
    assert "def add(a,b): return 0" in str(tool["content"])


def test_native_turn_growth_is_bounded_by_new_results_not_state_rebuilds(
    native_run: tuple[list[dict[str, object]], RunStatus],
) -> None:
    payloads, _ = native_run
    sizes = [len(json.dumps(p["messages"]).encode()) for p in payloads]
    first_turn = sizes[0]
    for before, after in pairwise(sizes):
        assert after - before < first_turn // 2, sizes


def test_reasoning_runs_on_new_evidence_and_plans_carry_follow_through_reads(
    native_run: tuple[list[dict[str, object]], RunStatus],
) -> None:
    payloads, _ = native_run
    # first turn, after read, after read -> think, fast, fast
    assert [p["think"] for p in payloads[:3]] == [True, False, False]
    plans = [str(m["content"]) for m in _messages(payloads[2]) if m["role"] == "assistant"]
    assert plans[0].startswith("Plan: read_file next")


def test_evidence_driven_thinking_requires_append_only_conversation() -> None:
    from loopforge.domain.prompts import default_controller_template  # noqa: PLC0415
    from loopforge.ports.model import ModelToolSpec  # noqa: PLC0415

    tool = ModelToolSpec(name="read_file", description="read", parameters={"type": "object"})
    with pytest.raises(ValueError, match="append-only"):
        ollama_model.OllamaModel(
            model="qwen",
            tools=(tool,),
            template=default_controller_template(),
            thinking=True,
            thinking_policy="on_evidence",
            conversation_mode="state",
        )


def test_a3b_model_gets_vendor_sampling_and_room_for_long_reasoning() -> None:
    from loopforge.adapters.ollama_profiles import profile_for  # noqa: PLC0415

    a3b = profile_for("coding", native=True, model="qwen3.6:35b-a3b")
    assert (a3b.temperature, a3b.top_p, a3b.presence_penalty) == (1.0, 0.95, 1.5)
    assert a3b.output_tokens == 16384
    assert (a3b.observation_format, a3b.read_page_chars) == ("delimited", 12000)
    assert a3b.context_tokens == 131072
    assert a3b.options()["presence_penalty"] == 1.5
    # Constant think flag and exact replay keep the hybrid model's prefix cache valid.
    assert a3b.thinking_policy == "always"
    assert a3b.preserve_thinking is True
    assert a3b.history_replay == "exact"
    dense = profile_for("coding", native=True, model="qwen3.8:27b-q4_K_M")
    assert (dense.temperature, dense.presence_penalty, dense.output_tokens) == (0.6, 0.0, 8192)
    assert (dense.observation_format, dense.read_page_chars) == ("plain", 3000)
    # The model never changes the bounded-v3 control profile.
    assert profile_for("coding", bounded_v2=True, model="qwen3.6:35b-a3b") == profile_for(
        "coding", bounded_v2=True
    )


def test_a3b_settings_reach_the_provider_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FakeChecks)
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> ollama_model.OllamaModel:
        captured.update(kwargs)
        return ollama_model.OllamaModel(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(coding, "OllamaModel", factory)
    repo = workspace(tmp_path)
    config = campaign_config(repo).model_copy(
        update={"inference_profile": "qwen-native-v1", "model": "qwen3.6:35b-a3b"}
    )
    coding.build_coding(config, contract(), repo, store=InMemoryEventStore())
    assert captured["temperature"] == 1.0
    assert captured["max_output_tokens"] == 16384
    assert captured["observation_format"] == "delimited"
    assert captured["context_window_tokens"] == 131072
    sampling = captured["sampling_options"]
    assert isinstance(sampling, dict)
    assert sampling["presence_penalty"] == 1.5
    assert "16384-token" in str(captured["template"])


@pytest.mark.parametrize(("engine", "budget"), [("ollama", 32768), ("mlx", 49152)])
def test_engine_selects_its_transport_and_usage_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str, budget: int
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FakeChecks)
    lab = object()

    def open_lab(path: Path) -> object:
        assert path.is_absolute()
        return lab

    monkeypatch.setattr(coding, "lab_transport", open_lab)
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> ollama_model.OllamaModel:
        captured.update(kwargs)
        return ollama_model.OllamaModel(**{**kwargs, "transport": None})  # type: ignore[arg-type]

    monkeypatch.setattr(coding, "OllamaModel", factory)
    repo = workspace(tmp_path)
    config = campaign_config(repo).model_copy(
        update={"inference_profile": "qwen-native-v1", "model": "qwen3.6:35b-a3b", "engine": engine}
    )
    coding.build_coding(config, contract(), repo, store=InMemoryEventStore())
    assert captured["working_budget_tokens"] == budget
    assert captured["transport"] is (lab if engine == "mlx" else None)
