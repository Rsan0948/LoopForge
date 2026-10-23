"""Frontier review adapter: reasoning room and memory of its own earlier reads. No network."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest

import loopforge.adapters.gemini_supervisor as gemini_module
from loopforge.adapters.gemini_review import REVIEW_OUTPUT_TOKENS, GeminiReviewModel
from loopforge.adapters.paid_budget import SQLitePaidBudget
from loopforge.domain.context import ContextItem, ContextSource, ModelContext
from loopforge.domain.security import TrustClass
from loopforge.domain.types import ContextItemId, RunId
from loopforge.ports.model import ModelToolSpec, ModelTurnError

READ = ModelToolSpec(
    name="read_file",
    description="Read actual source",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "start_line": {"type": "string"}},
        "required": ["path"],
    },
)


@pytest.fixture(autouse=True)
def fixture_pricing_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini_module, "PRICE_VALID_UNTIL", datetime(2100, 1, 1, tzinfo=UTC))


def _reply(output_tokens: int, finish: str = "STOP") -> dict[str, object]:
    action = {"tool_name": "read_file", "arguments": {"path": "app/server.py"}}
    return {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps(action)}]}, "finishReason": finish}
        ],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 40,
            "thoughtsTokenCount": output_tokens - 40,
            "totalTokenCount": 100 + output_tokens,
        },
    }


def _context(observation: str | None) -> ModelContext:
    now = datetime.now(UTC)
    items = [
        ContextItem(
            item_id=ContextItemId("run_r:objective"),
            content="Review the patch",
            trust=TrustClass.AUTHORIZED_HUMAN,
            source=ContextSource(origin=TrustClass.AUTHORIZED_HUMAN, reference="objective"),
            created_at=now,
        )
    ]
    if observation is not None:
        items.append(
            ContextItem(
                item_id=ContextItemId("run_r:7:observation"),
                content=observation,
                trust=TrustClass.UNTRUSTED_CONTENT,
                source=ContextSource(origin=TrustClass.UNTRUSTED_CONTENT, reference="read"),
                created_at=now,
            )
        )
    return ModelContext(run_id=RunId("run_r"), items=tuple(items), assembled_at=now)


def _reviewer(
    tmp_path: Path, transport: httpx.MockTransport
) -> tuple[GeminiReviewModel, httpx.Client]:
    client = httpx.Client(transport=transport)
    budget = SQLitePaidBudget(tmp_path / "paid.sqlite", 2_000_000)
    return GeminiReviewModel(
        "fixture-review-key", budget, (READ,), tmp_path / "traces", client=client
    ), client


def test_reasoning_beyond_4096_tokens_is_accepted(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_reply(6000))

    reviewer, client = _reviewer(tmp_path, httpx.MockTransport(respond))
    with client:
        turn = reviewer.propose_action(_context(None))
    assert turn.action.tool_name == "read_file"
    config = requests[0]["generationConfig"]
    assert isinstance(config, dict)
    assert config["maxOutputTokens"] == REVIEW_OUTPUT_TOKENS == 8192


def test_output_above_the_review_cap_still_fails_closed(tmp_path: Path) -> None:
    reviewer, client = _reviewer(
        tmp_path, httpx.MockTransport(lambda _: httpx.Response(200, json=_reply(9000)))
    )
    with client, pytest.raises(ModelTurnError):
        reviewer.propose_action(_context(None))


def test_reviewer_sees_its_earlier_reads_when_reporting(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_reply(500))

    reviewer, client = _reviewer(tmp_path, httpx.MockTransport(respond))
    with client:
        reviewer.propose_action(_context(None))
        reviewer.propose_action(_context("Source 'app/server.py' 1: return JSONResponse(200)"))
        reviewer.propose_action(_context("Source 'app/tests/test_api.py' 57: assert 422"))

    def packet(index: int) -> dict[str, object]:
        contents = cast("list[dict[str, list[dict[str, str]]]]", requests[index]["contents"])
        return json.loads(contents[0]["parts"][0]["text"])

    assert packet(0)["your_earlier_actions_and_results_untrusted"] == []
    history = cast(
        "list[dict[str, object]]", packet(2)["your_earlier_actions_and_results_untrusted"]
    )
    assert [h["result"] for h in history] == [
        "Source 'app/server.py' 1: return JSONResponse(200)",
        "Source 'app/tests/test_api.py' 57: assert 422",
    ]
    assert history[0]["tool"] == "read_file"


def test_review_request_limit_bounds(tmp_path: Path) -> None:
    budget = SQLitePaidBudget(tmp_path / "paid.sqlite", 1000)
    for value in (15_999, 400_001):
        with pytest.raises(ValueError, match="bounds"):
            GeminiReviewModel(
                "fixture-review-key", budget, (READ,), tmp_path, max_request_bytes=value
            )
