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
        return real(**kwargs, transport=httpx.MockTransport(transport))  # type: ignore[arg-type]

    monkeypatch.setattr(coding, "OllamaModel", factory)
    config = campaign_config(repo).model_copy(update={"inference_profile": "qwen-native-v1"})
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
