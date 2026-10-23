"""Offline behavioral regressions for the model-performance implementation."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from loopforge.adapters.check_receipts import CheckReceiptSandbox
from loopforge.adapters.container_sandbox import ContainerSandbox, ContainerSandboxConfig
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.model_lease import ModelLease
from loopforge.adapters.scripted import ScriptedModel
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext
from loopforge.domain.types import ActionId, RunId
from loopforge.entrypoints.campaign_evidence import missing_coverage, required_reads, source_page
from loopforge.entrypoints.campaign_models import (
    CampaignConfig,
    Evidence,
    Finding,
    ReviewReport,
    ShortReferencedFinding,
    scalar_contract,
)
from loopforge.entrypoints.campaign_navigation import find_symbol, repository_map
from loopforge.entrypoints.campaign_qualification import grade_contract
from loopforge.entrypoints.campaign_quality import coverage_counts, plan_workload
from loopforge.entrypoints.campaign_review import (
    ReportingReviewModel,
    ReviewTools,
    preflight_report,
    report_schema,
)
from loopforge.ports.model import ModelTurnError
from loopforge.ports.sandbox import SandboxCommandResult, SandboxPolicyError
from loopforge.ports.tools import ToolExecutionRequest


def read(tools: ReviewTools, **args: str):
    return tools.execute(
        ToolExecutionRequest(
            proposal=ActionProposal(ActionId("read"), "read_file", args),
            attempt=1,
            timeout_seconds=5,
        )
    )


def test_long_line_reachable_and_partial_read_cannot_complete_coverage() -> None:
    content = "a" * 3000 + "important_tail" * 200
    tools = ReviewTools({"long.py": content}, "", max_source_reads=4)
    tools.required_intervals = {"long.py": [(1, 1)]}
    first = read(tools, path="long.py")
    assert first.ok
    assert "start_column: 3000" in first.observation
    assert missing_coverage(tools.excerpts, tools.required_intervals)
    second = read(tools, path="long.py", start_line="1", start_column="3000")
    assert second.ok
    assert "important_tail" in second.observation
    assert not missing_coverage(tools.excerpts, tools.required_intervals)
    assert tools.excerpts[("long.py", 1)] == content
    observed = tools.reporting_observations()
    assert any(o["text"] == content for o in observed)
    assert not read(tools, path="long.py", start_line="1", start_column="99999").ok


def test_empty_source_and_exact_continuations() -> None:
    assert source_page("").empty
    tools = ReviewTools({"empty.py": ""}, "")
    assert read(tools, path="empty.py").ok
    assert not missing_coverage(tools.excerpts, {"empty.py": [(1, 0)]})
    assert required_reads({"empty.py": ""}, {"empty.py": [(1, 0)]}) == 1
    with pytest.raises(ValueError, match="selector"):
        source_page("x", 0)


def test_actual_report_serializer_admits_small_and_denies_oversized_mandatory_evidence() -> None:
    preflight_report({"a.py": "return 1"}, {"a.py": [(1, 1)]}, "Inspect behavior", 4)
    files = {f"{i}.py": "\n".join("x" * 100 for _ in range(30)) for i in range(12)}
    with pytest.raises(ModelTurnError, match="cannot fit"):
        preflight_report(files, {p: [(1, 30)] for p in files}, "Inspect changes", 64)
    unicode_files = {"u.py": "🙂" * 5000}
    with pytest.raises(ModelTurnError):
        preflight_report(unicode_files, {"u.py": [(1, 1)]}, "Inspect", 64)


def test_doc_claim_paired_with_actual_implementation_and_unpaired_disclosed() -> None:
    files = {
        "current.md": "REQUEST_TIMEOUT_SECONDS in impl.py is 30.",
        "impl.py": "REQUEST_TIMEOUT_SECONDS = 10",
    }
    workload = plan_workload(files, ".", "docs", 1)
    assert {"current.md", "impl.py"} <= workload.obligations.keys()
    unpaired = plan_workload({"current.md": "A vague claim without source."}, ".", "docs", 1)
    assert any("Unpaired" in value for value in unpaired.limitations)


def test_coverage_avoids_repeated_files_and_does_not_count_duplicate_intervals() -> None:
    details = [
        json.dumps(
            {
                "revision": "a" * 40,
                "source_excerpts": [{"path": "a.py", "start_line": 1, "end_line": 3}],
            }
        )
    ] * 3
    assert coverage_counts(details, "a" * 40) == {"a.py": 1}
    assert coverage_counts(details, "b" * 40) == {}
    files = {"a.py": "def a():\n    raise ValueError()", "z.js": "function z() { throw Error(); }"}
    workload = plan_workload(files, ".", "bugs", 1, coverage={"a.py": 10})
    assert "z.js" in workload.obligations


def test_navigation_is_bounded_content_bound_and_not_source_evidence() -> None:
    files = {"a.py": "def special():\n    return 1\n", "b.py": "special()"}
    result = json.loads(repository_map(files, "special"))
    assert result["files"][0]["path"] == "a.py"
    assert result["files"][0]["sha256"]
    changed = json.loads(repository_map({"a.py": "def changed(): pass"}, "special"))
    assert changed["files"][0]["sha256"] != result["files"][0]["sha256"]
    assert len(repository_map({f"{i}.py": "def x(): pass" for i in range(100)}, "x")) <= 6000
    assert len(json.loads(find_symbol(files, "special"))["matches"]) == 2
    with pytest.raises(ValueError, match="identifier"):
        find_symbol(files, "../bad")


def test_symbol_navigation_prioritizes_definitions_and_pages_references() -> None:
    files = {
        f"a_{index:02d}.ts": "const caller = createSupabaseServerClient();\n" for index in range(35)
    }
    files["z_auth.ts"] = (
        "export async function createSupabaseServerClient() {\n  return client;\n}\n"
    )
    files["z_client.ts"] = (
        "export const researchClient = {\n"
        "  listRightEvents(\n"
        "    entityId: string,\n"
        "  ): Promise<Result> {\n"
        "    return getJson(entityId);\n"
        "  },\n"
        "};\n"
    )
    first = json.loads(find_symbol(files, "createSupabaseServerClient"))
    assert first["matches"][0] == {
        "path": "z_auth.ts",
        "start_line": 1,
        "end_line": 3,
        "kind": "definition",
    }
    assert first["capped"]
    assert first["next_offset"] == 30
    second = json.loads(find_symbol(files, "createSupabaseServerClient", offset=30))
    assert len(second["matches"]) == 6
    assert second["next_offset"] is None
    method = json.loads(find_symbol(files, "listRightEvents"))
    assert method["matches"][0] == {
        "path": "z_client.ts",
        "start_line": 2,
        "end_line": 6,
        "kind": "definition",
    }
    with pytest.raises(ValueError, match="offset"):
        find_symbol(files, "listRightEvents", offset=-1)


def test_model_lease_excludes_independent_clients_and_releases(tmp_path: Path) -> None:
    first, second = ModelLease(tmp_path / "model.lock"), ModelLease(tmp_path / "model.lock")
    first.acquire("campaign")
    try:
        with pytest.raises(ValueError, match="owns"):
            second.acquire("builder")
    finally:
        first.release()
    second.acquire("builder")
    second.release()


def test_check_receipts_are_one_use_and_invalidate_all_inputs(tmp_path: Path) -> None:

    class Checks(ContainerSandbox):
        calls = 0
        passed = True

        def run(
            self, command_name: str, *, timeout_seconds: float | None = None
        ) -> SandboxCommandResult:
            del command_name, timeout_seconds
            self.calls += 1
            return SandboxCommandResult(
                exit_code=0 if self.passed else 1, stdout="", stderr="", succeeded=self.passed
            )

    raw = Checks(tmp_path, config=ContainerSandboxConfig(image="sha256:" + "a" * 64, commands=()))
    (tmp_path / "source").write_text("a")
    wrapped = CheckReceiptSandbox(
        raw, tmp_path, {"image": "a", "environment": {}}, frozenset({"check"})
    )
    wrapped.run("check")
    wrapped.run("check")
    assert raw.calls == 1
    assert wrapped.reused == 1
    wrapped.run("check")
    assert raw.calls == 2  # A receipt can be consumed only once.
    (tmp_path / "source").write_text("b")
    wrapped.run("check")
    assert raw.calls == 3
    wrapped.run("other")
    wrapped.run("other")
    assert raw.calls == 5
    changed = CheckReceiptSandbox(
        raw, tmp_path, {"image": "b", "environment": {"X": "1"}}, frozenset({"check"})
    )
    changed.run("check")
    assert raw.calls == 6  # A fresh reviewer cannot inherit coder receipts.
    raw.passed = False
    changed.receipts.clear()
    changed.run("check")
    changed.run("check")
    assert raw.calls == 8
    raw.passed = True
    timed = CheckReceiptSandbox(raw, tmp_path, {}, frozenset({"check"}), {"check": 10})
    timed.run("check")
    for invalid in (-1, 0, float("nan"), float("inf")):
        with pytest.raises(SandboxPolicyError, match="finite and positive"):
            timed.run("check", timeout_seconds=invalid)
    assert raw.calls == 9
    timed.run("check", timeout_seconds=20)
    assert raw.calls == 9  # Same effective code-owned timeout.
    timed.run("check", timeout_seconds=5)
    assert raw.calls == 10  # Different effective timeout needs a fresh check.
    with pytest.raises(ValueError, match="outside source"):
        required_reads({"x": "a"}, {"x": [(1, 2)]})


def test_gold_grader_rejects_wrong_claim_and_reversed_direction() -> None:
    files = {"current.md": "MAX_RETRIES in impl.py is 2.", "impl.py": "MAX_RETRIES = 4"}

    def report(explanation: str) -> ReviewReport:
        return ReviewReport(
            summary="Checked",
            findings=[
                Finding(
                    title="MAX_RETRIES contradiction",
                    disposition="suspected",
                    severity="medium",
                    explanation=explanation,
                    evidence=[Evidence(path=p, quote=t) for p, t in files.items()],
                    next_check="Correct docs",
                )
            ],
            unresolved=[],
        )

    assert grade_contract(
        report("Expected: MAX_RETRIES is 2.\nObserved: MAX_RETRIES is 4."), True, files
    )
    assert not grade_contract(
        report("Expected: MAX_RETRIES is 4.\nObserved: MAX_RETRIES is 2."), True, files
    )
    assert not grade_contract(report("Unrelated constant is incorrect."), True, files)
    assert not grade_contract(report("MAX_RETRIES 2 and 4"), True, files)


def test_report_schema_removes_unneeded_column_arithmetic() -> None:
    short = ReviewTools({"x": "small"}, "")
    assert read(short, path="x").ok
    schema = report_schema(short)
    assert "quote_start" not in json.dumps(schema)
    long = ReviewTools({"x": "a" * 2000}, "")
    assert read(long, path="x").ok
    assert "quote_start" in json.dumps(report_schema(long))


def test_required_evidence_scheduling_is_bounded_and_journalable() -> None:
    tools = ReviewTools({"long.py": "x" * 6001}, "", max_source_reads=2)
    tools.required_intervals = {"long.py": [(1, 1)]}
    model = ReportingReviewModel(
        ScriptedModel([]),
        tools,
        CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."]),
        InMemoryEventStore(),
    )
    context = ModelContext(run_id=RunId("required"), items=(), assembled_at=datetime.now(UTC))
    for column in (0, 3000):
        turn = model.propose_action(context)
        assert turn.action.tool_name == "read_file"
        assert turn.action.arguments["start_column"] == str(column)
        assert tools.execute(ToolExecutionRequest(turn.action, 1, 5)).ok
    with pytest.raises(ModelTurnError, match="immutable source read allowance"):
        model.propose_action(context)
    assert missing_coverage(tools.excerpts, tools.required_intervals)


def test_documentation_pair_locates_late_claim_instead_of_only_header() -> None:
    files = {
        "current.md": "Current contract.\n"
        + "Unrelated introduction.\n" * 90
        + "MAX_WAIT_SECONDS in impl.py is configured as 30.\n",
        "impl.py": "MAX_WAIT_SECONDS = 10",
    }
    workload = plan_workload(files, "current.md", "docs", 1)
    assert any(start <= 92 <= end for start, end in workload.obligations["current.md"])
    assert "impl.py" in workload.obligations


def test_preflight_preserves_required_lines_without_extra_page_bloat() -> None:
    files = {f"{i}.py": "needed()\n" + "x" * 2900 for i in range(12)}
    obligations = {path: [(1, 1)] for path in files}
    # Actual reads include nearly 3000 irrelevant characters each; the report
    # retains all mandatory lines and IDs without copying irrelevant page tails.
    preflight_report(files, obligations, "Inspect only needed()", 20)


def test_same_scalar_contract_is_not_a_defect_but_different_values_are() -> None:
    value = {
        "title": "Timeout contradiction",
        "disposition": "suspected",
        "severity": "low",
        "explanation": "Compare the constant",
        "next_check": "Check source",
        "expected_behavior": "REQUEST_TIMEOUT_SECONDS in impl.py is 10.",
        "observed_behavior": "REQUEST_TIMEOUT_SECONDS = 10",
        "evidence": [
            {"excerpt_id": "e1", "start_line": 1, "line_count": 1},
            {"excerpt_id": "e2", "start_line": 1, "line_count": 1},
        ],
    }
    with pytest.raises(ValidationError, match="same scalar constant"):
        ShortReferencedFinding.model_validate(value)
    value["observed_behavior"] = "REQUEST_TIMEOUT_SECONDS = 30"
    assert ShortReferencedFinding.model_validate(value).disposition == "suspected"
    value["observed_behavior"] = "REQUEST_TIMEOUT_SECONDS is 10 minutes"
    # Units may change meaning: this narrow control must not guess equivalence.
    assert ShortReferencedFinding.model_validate(value).disposition == "suspected"


def test_scalar_contract_narrative_wrappers_do_not_hide_matching_values() -> None:
    assert scalar_contract(
        "The current documentation (current.md) states that LIMIT in impl.py is 10."
    ) == ("LIMIT", "10")
    assert scalar_contract("The implementation file (impl.py) defines LIMIT as 10.") == (
        "LIMIT",
        "10",
    )
    for text in (
        "LIMIT is 10 minutes",
        "LIMIT = 10 in seconds",
        "LIMIT is not 10",
        "If enabled LIMIT is 10",
        "The minimum LIMIT is 10",
        "OTHER differs; LIMIT is 10",
        "After 2 attempts LIMIT is 10",
    ):
        assert scalar_contract(text) is None
