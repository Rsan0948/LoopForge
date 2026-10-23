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
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.paid_budget import SQLitePaidBudget
from loopforge.adapters.scripted import ScriptedModel
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import ActionProposed, RunStopped
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunStatus
from loopforge.entrypoints.campaign_execution import PatchReviewVerifier
from loopforge.entrypoints.campaign_models import CodingTaskConfig, ReviewReport
from loopforge.entrypoints.campaign_review import ReviewTools, build_review, tool_specs
from loopforge.ports.model import ModelTurnError
from loopforge.ports.tools import ToolExecutionRequest, ToolResult
from loopforge.ports.verifier import VerificationResult
from tests.unit.test_campaign_coding import campaign_config, contract, workspace
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


@pytest.mark.parametrize("blocker", ["finding", "unresolved", "checks", "clean"])
def test_completed_patch_review_never_pays_to_resubmit(blocker: str, tmp_path: Path) -> None:

    class Checks:
        def verify(self, state: RunState) -> VerificationResult:
            del state
            return VerificationResult(passed=blocker != "checks", summary="fixed checks")

    report: dict[str, object] = {
        "summary": "Complete source review",
        "findings": [],
        "unresolved": [],
    }
    if blocker == "finding":
        report["findings"] = [
            {
                "title": "Real defect",
                "disposition": "suspected",
                "severity": "medium",
                "explanation": "Source shows a defect",
                "evidence": [{"path": "a.py", "quote": "bad()"}],
                "next_check": "Repair then verify",
            }
        ]
    if blocker == "unresolved":
        report["unresolved"] = ["Contract question remains"]
    actions = [
        ActionProposal(ActionId("read"), "read_file", {"path": "a.py"}),
        ActionProposal(ActionId("report"), "submit_report", {"report": json.dumps(report)}),
        ActionProposal(ActionId("duplicate"), "submit_report", {"report": json.dumps(report)}),
    ]
    store = InMemoryEventStore()
    runtime, tools, _ = build_review(
        campaign_config(workspace(tmp_path)),
        files={"a.py": "bad()"},
        previous="",
        store=store,
        model=ScriptedModel(actions),
        patch_review=True,
    )
    runtime.verifier = PatchReviewVerifier(tools, Checks(), {"a.py"})
    run = runtime.start("Review this candidate")
    result = runtime.resume(run)
    events = store.events_for(run)
    assert result.status == (RunStatus.SUCCEEDED if blocker == "clean" else RunStatus.FAILED)
    assert tools.report is not None
    assert len([e for e in events if isinstance(e, ActionProposed)]) == 2
    if blocker != "clean":
        assert any(
            isinstance(e, RunStopped) and "REVIEW_REPAIR_REQUIRED" in e.summary for e in events
        )


@pytest.mark.parametrize(
    "initial",
    ['{"broken":true}', '{"summary":"Incomplete","findings":[],"unresolved":["Unread source"]}'],
)
def test_invalid_or_uncovered_report_can_be_corrected(initial: str, tmp_path: Path) -> None:
    class Checks:
        def verify(self, state: RunState) -> VerificationResult:
            del state
            return VerificationResult(passed=True, summary="fixed checks")

    clean = json.dumps({"summary": "Source reviewed", "findings": [], "unresolved": []})
    store = InMemoryEventStore()
    runtime, tools, _ = build_review(
        campaign_config(workspace(tmp_path)),
        files={"a.py": "bad()"},
        previous="",
        store=store,
        patch_review=True,
        model=ScriptedModel(
            [
                ActionProposal(ActionId("initial"), "submit_report", {"report": initial}),
                ActionProposal(ActionId("read"), "read_file", {"path": "a.py"}),
                ActionProposal(ActionId("corrected"), "submit_report", {"report": clean}),
            ]
        ),
    )
    runtime.verifier = PatchReviewVerifier(tools, Checks(), {"a.py"})
    run = runtime.start("Review this candidate")
    assert runtime.resume(run).status == RunStatus.SUCCEEDED
    assert len([e for e in store.events_for(run) if isinstance(e, ActionProposed)]) == 3


def _report(disposition: str, unresolved: list[str] | None = None) -> ReviewReport:
    # Mirrors the real blocking report from the 2026-10-23 full trial.
    return ReviewReport.model_validate(
        {
            "summary": "Reviewed CAMPAIGN_PATCH.diff and changed files.",
            "findings": [
                {
                    "title": "test_upload_idempotent_and_conflict removes 409 conflict assertion",
                    "disposition": disposition,
                    "severity": "medium",
                    "explanation": "The conflict path is no longer asserted.",
                    "evidence": [{"path": "app/tests/test_api.py", "quote": "assert r3"}],
                    "next_check": "Assert 409 for a same-job document with different bytes",
                }
            ],
            "unresolved": unresolved or [],
        }
    )


def _exhausted(report: ReviewReport) -> ReviewTools:
    tools = _tools(1)
    _call(tools, "read_file", path="app/a.py")
    tools.report = report
    return tools


def test_valid_blocking_report_ends_review_as_changes_requested_once_allowance_is_used() -> None:
    tools = _tools(1)
    _call(tools, "read_file", path="app/a.py")
    tools.report = _report("suspected")
    assert tools.review_decided() is None  # allowance remains: it may still settle the doubt
    _call(tools, "list_files")
    decided = tools.review_decided()
    assert decided is not None
    assert decided.startswith("Review requested changes: test_upload_idempotent")
    unresolved = _exhausted(_report("rejected", ["could not confirm launcher reuse"]))
    _call(unresolved, "list_files")
    assert "unresolved: could not confirm launcher reuse" in str(unresolved.review_decided())


def test_clean_or_missing_report_never_ends_review_as_changes_requested() -> None:
    clean = _exhausted(_report("rejected"))
    _call(clean, "list_files")
    assert clean.report_required()
    assert clean.review_decided() is None
    missing = _tools(1)
    _call(missing, "read_file", path="app/a.py")
    _call(missing, "list_files")
    assert missing.review_decided() is None
    assert _tools(None).review_decided() is None


def test_gemini_reviewer_stops_before_another_paid_call_once_changes_are_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gemini_module, "PRICE_VALID_UNTIL", datetime(2100, 1, 1, tzinfo=UTC))
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_reply(200))

    tools = _exhausted(_report("suspected"))
    _call(tools, "list_files")
    budget = SQLitePaidBudget(tmp_path / "paid.sqlite", 1_000_000)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        reviewer = GeminiReviewModel(
            "fixture-review-key", budget, tool_specs(), tmp_path / "traces", client=client
        )
        reviewer.progress = tools
        with pytest.raises(ModelTurnError) as stopped:
            reviewer.propose_action(_context(None))
    assert stopped.value.reason_code == "REVIEW_CHANGES_REQUESTED"
    assert calls == []
    assert budget.account().spent == 0
