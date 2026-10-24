"""Audit regressions: independent boundary, failure and evidence fixtures."""

import json
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from pydantic import ValidationError

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.ollama_reporting import OllamaReporting
from loopforge.adapters.scripted import ScriptedModel
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ModelContext, ModelRole
from loopforge.domain.types import ActionId, RunId
from loopforge.entrypoints.campaign_evidence import required_reads, review_obligations
from loopforge.entrypoints.campaign_execution import coding_feedback
from loopforge.entrypoints.campaign_models import CampaignConfig, ShortReferencedReport
from loopforge.entrypoints.campaign_review import (
    REVIEW_TEMPLATE,
    DeadlineAwareModel,
    ReviewTools,
    build_review,
    tool_specs,
)
from loopforge.entrypoints.campaign_source_links import import_edges
from loopforge.ports.model import ModelTurnError
from loopforge.ports.tools import ToolExecutionRequest


def config() -> CampaignConfig:
    return CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])


def snapshot_fixture(cfg: CampaignConfig) -> dict[str, str]:
    del cfg
    return {"a.py": "actual()"}


def context(run_id: RunId | None = None) -> ModelContext:
    return ModelContext(
        run_id=run_id or RunId("fixture"),
        items=(),
        assembled_at=datetime.now(UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )


@pytest.mark.parametrize(
    "counts",
    [
        {},
        {"eval_count": 2},
        {"prompt_eval_count": 2},
        {"prompt_eval_count": -1, "eval_count": 2},
        {"prompt_eval_count": 2, "eval_count": "3"},
        {"prompt_eval_count": True, "eval_count": 2},
    ],
)
@pytest.mark.parametrize("reporter", [False, True])
def test_unknown_usage_never_returns_an_action_or_report(
    counts: dict[str, object], reporter: bool
) -> None:
    payload = {
        "done": True,
        "message": {
            "role": "assistant",
            "content": "{}",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}],
        },
        **counts,
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    if reporter:
        adapter = OllamaReporting("fixture", transport=transport)
        try:
            with pytest.raises(ModelTurnError) as caught:
                adapter.generate("{}", {})
            assert caught.value.usage is None
        finally:
            adapter.close()
    else:
        model = OllamaModel(
            model="fixture", tools=tool_specs(), template=REVIEW_TEMPLATE, transport=transport
        )
        try:
            with pytest.raises(ModelTurnError) as caught:
                model.propose_action(context())
            assert caught.value.usage is None
        finally:
            model.close()


def test_explicit_zero_usage_is_valid_and_request_timing_is_observable() -> None:
    observed: list[dict[str, object]] = []
    adapter = OllamaReporting("fixture")

    def response(_: httpx.Request) -> httpx.Response:
        observed.append(adapter.request_tracker.snapshot())
        return httpx.Response(
            200,
            json={
                "done": True,
                "message": {"content": "{}"},
                "prompt_eval_count": 0,
                "eval_count": 0,
            },
        )

    adapter.client.close()
    adapter.client = httpx.Client(
        base_url="http://fixture", transport=httpx.MockTransport(response)
    )
    try:
        assert adapter.generate("{}", {}).usage.output_tokens == 0
        assert observed[0]["phase"] == "waiting_for_provider"
        assert observed[0]["timeout_seconds"] == 120
        assert adapter.request_tracker.snapshot()["phase"] == "idle"
    finally:
        adapter.close()


def short_report() -> dict[str, object]:
    return {
        "summary": "bounded fixture",
        "findings": [
            {
                "title": "Silent partial export",
                "disposition": "suspected",
                "severity": "medium",
                "explanation": "Failure skipped",
                "next_check": "Exercise a failed upstream response",
                "expected_behavior": "Surface failure",
                "observed_behavior": "Continue then return success",
                "evidence": [
                    {"excerpt_id": "e1", "start_line": 1, "line_count": 1},
                    {"excerpt_id": "e2", "start_line": 1, "line_count": 1},
                ],
            }
        ],
        "unresolved": [],
    }


@pytest.mark.parametrize(
    "claim", ["Consistent layout", "Signed-in demo exposure", "Missing prototype"]
)
def test_descriptive_or_absence_only_findings_lack_required_defect_basis(claim: str) -> None:
    data = short_report()
    data["findings"] = [
        {
            "title": claim,
            "disposition": "suspected",
            "severity": "low",
            "explanation": "Unsupported description",
            "next_check": "Inspect source",
            "evidence": [{"excerpt_id": "e1", "start_line": 1, "line_count": 1}],
        }
    ]
    with pytest.raises(ValidationError, match="expected/observed"):
        ShortReferencedReport.model_validate(data)
    assert ShortReferencedReport.model_validate(
        {"summary": "Observation only", "findings": [], "observations": [claim], "unresolved": []}
    ).observations == [claim]


def test_nonlocal_defect_exact_long_line_and_overflow_preserved() -> None:
    tools = ReviewTools(
        {
            "producer.ts": "x" * 1100 + "if (!ok) continue;" + "y" * 100,
            "consumer.ts": "return success();",
        },
        "",
    )
    for path in tools.files:
        assert tools.execute(
            ToolExecutionRequest(ActionProposal(ActionId(path), "read_file", {"path": path}), 1, 5)
        ).ok
    data = short_report()
    data["unresolved"] = ["missing evidence"] * 6
    data["overflow_checks"] = ["Check a separate cancellation branch"] * 6
    parsed = ShortReferencedReport.model_validate(data)
    with pytest.raises(ValueError, match="Maximum is 1000"):
        tools.referenced_report(parsed.referenced())

    def selection(start: int, length: int) -> ShortReferencedReport:
        return ShortReferencedReport.model_validate(
            parsed.model_dump()
            | {
                "findings": [
                    parsed.findings[0].model_dump()
                    | {
                        "evidence": [
                            parsed.findings[0].evidence[0].model_dump()
                            | {"quote_start": start, "quote_length": length},
                            parsed.findings[0].evidence[1].model_dump(),
                        ]
                    }
                ]
            }
        )

    report = tools.referenced_report(selection(1100, 18).referenced())
    assert report.findings[0].evidence[0].quote == "if (!ok) continue;"
    assert len(report.unresolved) == 12
    with pytest.raises(ValueError, match="exceeds observed"):
        tools.referenced_report(selection(1200, 200).referenced())


def test_feedback_oversized_diagnostic_and_many_whole_records_preserve_current_identity() -> None:
    details = [json.dumps({"run_id": "old", "evidence": "a" * 2000}) for _ in range(20)]
    details.append(
        json.dumps(
            {
                "run_id": "current",
                "patch_sha256": "b" * 64,
                "last_verification": "failing check",
                "evidence": "界" * 20000,
            }
        )
    )
    result = json.loads(coding_feedback(details))
    assert result["records"][-1]["patch_sha256"] == "b" * 64
    assert result["records"][-1]["last_verification"] == "failing check"
    assert result["records"][-1]["diagnostics_compacted"]
    assert result["omitted_records"] > 0
    assert len(json.dumps(result)) < 12000


def test_deadline_clamps_provider_wait_and_expired_run_never_calls_provider() -> None:
    calls: list[dict[str, float | None]] = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(cast("dict[str, float | None]", request.extensions["timeout"]))
        return httpx.Response(
            200,
            json={
                "done": True,
                "prompt_eval_count": 1,
                "eval_count": 1,
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
                    ],
                },
            },
        )

    adapter = OllamaModel(
        model="fixture",
        tools=tool_specs(),
        template=REVIEW_TEMPLATE,
        transport=httpx.MockTransport(response),
    )
    store = InMemoryEventStore()
    runtime, _, _ = build_review(
        config(), files={}, previous="", store=store, model=ScriptedModel([])
    )
    rid = runtime.start("deadline fixture")
    cfg = config().model_copy(update={"max_seconds": 10, "model_timeout_seconds": 600})
    model = DeadlineAwareModel(adapter, store, cfg)
    try:
        assert model.propose_action(context(rid)).action.tool_name == "read_file"
        timeout = calls[0]["read"]
        assert timeout is not None
        assert 0 < timeout <= 10
        expired = config().model_copy(update={"max_seconds": 0})
        with pytest.raises(ModelTurnError, match="deadline"):
            DeadlineAwareModel(adapter, store, expired).propose_action(context(rid))
        assert len(calls) == 1
    finally:
        adapter.close()


def test_static_python_imports_resolve_real_imports_and_never_match_docstring_mentions() -> None:
    files = {
        "apps/api/audit_log.py": "def record():\n    raise ValueError()",
        "apps/api/dependencies/__init__.py": "def require_key():\n    return False",
        "tests/api/test_audit_log.py": (
            "import apps.api.audit_log as audit\n\ndef test_failure():\n    audit.record()"
        ),
        "tests/api/test_auth.py": (
            "from apps.api.dependencies import require_key\n\n"
            "def test_denied():\n    assert not require_key()"
        ),
        "tests/nodes/test_population.py": (
            "'''__init__.py audit_log.py mentions are not imports'''\n"
            "def test_population():\n    assert True"
        ),
    }
    assert "apps/api/audit_log.py" in import_edges(files, "tests/api/test_audit_log.py")
    assert "apps/api/dependencies/__init__.py" in import_edges(files, "tests/api/test_auth.py")
    assert import_edges(files, "tests/nodes/test_population.py") == {}
    files["apps/api/consumer.py"] = "from .audit_log import record"
    assert "apps/api/audit_log.py" in import_edges(files, "apps/api/consumer.py")


def test_typescript_import_links_exclude_comments_and_template_examples() -> None:
    files = {
        "src/main.ts": (
            '/*\nimport { fake } from "./fake";\n*/\n'
            '// import { fake } from "./fake";\n'
            '`import { fake } from "./fake";`;\n'
            'const url = "https://example.invalid/";\n'
            'import { real } from "./real";\n'
        ),
        "src/fake.ts": "export const fake = 1;\n",
        "src/real.ts": "export const real = 1;\n",
    }
    assert import_edges(files, "src/main.ts") == {"src/real.ts": ["real"]}


def test_typescript_import_links_use_export_names_without_alias_keywords() -> None:
    files = {
        "src/main.ts": 'import { run as local, type Options } from "./worker";\n',
        "src/worker.ts": "export const run = () => 1;\nexport type Options = {};\n",
    }
    assert import_edges(files, "src/main.ts") == {"src/worker.ts": ["run", "Options"]}
    files["src/main.ts"] = 'import * as worker from "./worker";\n'
    assert import_edges(files, "src/main.ts") == {"src/worker.ts": []}


def test_partial_tail_deletion_reviews_surviving_context_and_diff() -> None:
    files = {
        "x.py": "a\nb\nc\n",
        "CAMPAIGN_PATCH.diff": "--- a/x.py\n+++ b/x.py\n@@ -4,1 +3,0 @@\n-removed\n",
    }
    obligations = review_obligations(files, set(files))
    assert obligations["x.py"] == [(3, 3)]
    assert obligations["CAMPAIGN_PATCH.diff"] == [(1, 4)]
    assert required_reads(files, obligations) == 2
    files["CAMPAIGN_PATCH.diff"] = "--- a/x.py\n+++ b/x.py\n@@ -2,1 +2,1 @@\n-old\n+new\n"
    assert review_obligations(files, set(files))["x.py"] == [(2, 2)]


def test_provider_defect_schema_requires_basis_fields_as_runtime_validator_does() -> None:
    schema = ShortReferencedReport.model_json_schema()
    required = schema["$defs"]["ShortReferencedFinding"]["required"]
    assert {"expected_behavior", "observed_behavior", "evidence", "next_check"} <= set(required)
    assert ShortReferencedReport.model_validate(
        {"summary": "No supported defect", "findings": [], "unresolved": []}
    )
    finding: dict[str, object] = {
        "title": "Pending state remains",
        "disposition": "suspected",
        "severity": "medium",
        "explanation": "Rejection exits before pending is reset.",
        "next_check": "Reproduce component rejection",
        "evidence": [
            {"excerpt_id": "e1", "start_line": 1, "line_count": 1},
            {"excerpt_id": "e1", "start_line": 2, "line_count": 1},
        ],
    }
    data = {
        "summary": "Conditional rejected-promise failure",
        "findings": [finding],
        "unresolved": [],
    }
    with pytest.raises(ValidationError, match="expected/observed"):
        ShortReferencedReport.model_validate(data)
    finding.update(
        expected_behavior="Rejected attempt permits retry",
        observed_behavior="Rejected attempt leaves button disabled",
    )
    assert ShortReferencedReport.model_validate(data).findings[0].expected_behavior
