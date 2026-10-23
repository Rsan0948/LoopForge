"""Code-owned review termination: after coverage, a frozen call allowance, then report."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import ValidationError

import loopforge.adapters.gemini_supervisor as gemini_module
from loopforge.adapters.gemini_review import GeminiReviewModel
from loopforge.adapters.paid_budget import SQLitePaidBudget
from loopforge.domain.actions import ActionProposal
from loopforge.domain.types import ActionId
from loopforge.entrypoints.campaign_models import CodingTaskConfig
from loopforge.entrypoints.campaign_review import ReviewTools, tool_specs
from loopforge.ports.model import ModelTurnError
from loopforge.ports.tools import ToolExecutionRequest, ToolResult
from tests.unit.test_campaign_coding import contract
from tests.unit.test_gemini_review import _context, _reply  # pyright: ignore[reportPrivateUsage]

FILES = {"app/a.py": "".join(f"line {i}\n" for i in range(1, 30)), "app/b.py": "x = 1\n"}


def _call(tools: ReviewTools, name: str, **arguments: str) -> ToolResult:
    return tools.execute(
        ToolExecutionRequest(ActionProposal(ActionId(f"c{tools.calls}"), name, arguments), 1, 5)
    )


def _tools(allowance: int | None) -> ReviewTools:
    tools = ReviewTools(FILES, "", max_source_reads=50)
    tools.required_intervals = {"app/a.py": [(1, 5)]}
    tools.post_coverage_calls = allowance
    return tools


def test_after_coverage_only_the_frozen_allowance_then_report_remains() -> None:
    tools = _tools(2)
    assert tools.review_status()["required_coverage"] != "complete"
    _call(tools, "list_files")  # pre-coverage navigation is not limited by the allowance
    _call(tools, "read_file", path="app/a.py")
    assert tools.coverage_complete()
    assert tools.covered_at == 2
    assert tools.review_status()["calls_remaining_before_report"] == 2
    _call(tools, "read_file", path="app/b.py")
    _call(tools, "list_files")
    assert tools.report_required()
    assert tools.offered_tools() == frozenset({"submit_report"})
    refused = _call(tools, "read_file", path="app/b.py")
    assert refused.ok is False
    assert refused.error_code == "REVIEW_REPORT_REQUIRED"
    assert tools.calls == 4  # refused calls execute nothing
    status = tools.review_status()
    assert status["report_required_now"] is True
    assert status["calls_remaining_before_report"] == 0


def test_without_an_allowance_review_stays_open_ended() -> None:
    tools = _tools(None)
    for _ in range(6):
        _call(tools, "read_file", path="app/a.py")
    assert tools.offered_tools() is None
    assert not tools.report_required()
    assert tools.review_status()["calls_remaining_before_report"] is None


def test_allowance_is_operator_frozen_and_omitted_when_unset() -> None:
    assert "max_review_calls_after_coverage" not in contract().model_dump(mode="json")
    task = CodingTaskConfig.model_validate(
        {**contract().model_dump(), "max_review_calls_after_coverage": 6}
    )
    assert json.loads(task.model_dump_json())["max_review_calls_after_coverage"] == 6
    for value in (0, 21, "6"):
        with pytest.raises(ValidationError):
            CodingTaskConfig.model_validate(
                {**contract().model_dump(), "max_review_calls_after_coverage": value}
            )


def test_gemini_reviewer_is_offered_only_the_report_tool_when_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gemini_module, "PRICE_VALID_UNTIL", datetime(2100, 1, 1, tzinfo=UTC))
    bodies: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 400})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_reply(200))

    tools = _tools(1)
    _call(tools, "read_file", path="app/a.py")
    _call(tools, "list_files")
    assert tools.report_required()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        reviewer = GeminiReviewModel(
            "fixture-review-key",
            SQLitePaidBudget(tmp_path / "paid.sqlite", 1_000_000),
            tool_specs(),
            tmp_path / "traces",
            client=client,
        )
        reviewer.progress = tools
        # The scripted reply names read_file, which is no longer offered: rejected.
        with pytest.raises(ModelTurnError):
            reviewer.propose_action(_context(None))
    config = cast(
        "dict[str, dict[str, dict[str, dict[str, list[str]]]]]", bodies[0]["generationConfig"]
    )
    assert config["responseJsonSchema"]["properties"]["tool_name"]["enum"] == ["submit_report"]
    contents = cast("list[dict[str, list[dict[str, str]]]]", bodies[0]["contents"])
    packet = json.loads(contents[0]["parts"][0]["text"])
    assert [t["name"] for t in packet["tools"]] == ["submit_report"]
    assert packet["review_status_runtime_measured"]["report_required_now"] is True
