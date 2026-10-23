"""Audit regressions: independent boundary, failure and evidence fixtures."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.ollama_reporting import OllamaReporting
from loopforge.adapters.scripted import ScriptedModel
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.domain.actions import ActionProposal
from loopforge.domain.campaign import CampaignEvent, CampaignState, reduce_campaign
from loopforge.domain.context import ModelContext, ModelRole
from loopforge.domain.types import ActionId, RunId
from loopforge.entrypoints import campaign as module
from loopforge.entrypoints.campaign_evidence import required_reads, review_obligations
from loopforge.entrypoints.campaign_execution import coding_feedback
from loopforge.entrypoints.campaign_models import CampaignConfig, ShortReferencedReport
from loopforge.entrypoints.campaign_quality import (
    FindingAssessment,
    ReviewUnitContract,
    assessment_context,
    plan_workload,
    related_source,
)
from loopforge.entrypoints.campaign_review import (
    REVIEW_TEMPLATE,
    DeadlineAwareModel,
    ReviewTools,
    build_review,
    tool_specs,
)
from loopforge.entrypoints.campaign_source_links import (
    definition_anchor,
    import_anchor,
    import_edges,
    matching_tests,
)
from loopforge.entrypoints.campaign_source_links import test_interval as case_interval
from loopforge.entrypoints.campaign_visibility import unknown_provider_usage
from loopforge.ports.model import ModelTurnError
from loopforge.ports.state_store import StateStorePort
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


def assessment(**changes: object) -> FindingAssessment:
    return FindingAssessment.model_validate(
        {
            "revision": "a" * 40,
            "claim": "Canonical plan table is contradictory",
            "disposition": "rejected",
            "evidence": "Pinned lines 67-72 explicitly supersede the dated snapshot.",
            "paths": ["docs/plan.md"],
            **changes,
        }
    )


def test_dispositions_are_append_only_idempotent_and_require_new_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "pinned_files", snapshot_fixture)
    with TestClient(module.create_app(tmp_path)) as client:
        cid = client.post("/campaigns", json=config().model_dump()).json()["id"]
        url = f"/campaigns/{cid}/assessments"
        record = assessment()
        assert client.post(url, json=record.model_dump()).status_code == 201
        assert client.post(url, json=record.model_dump()).status_code == 201
        changed = record.model_dump() | {"disposition": "suspected"}
        assert client.post(url, json=changed).status_code == 409
        assert client.post(url, json=changed | {"supersedes": record.identity}).status_code == 409
        assert (
            client.post(
                url,
                json=changed
                | {
                    "supersedes": record.identity,
                    "evidence": "New pinned source removes the supersession notice.",
                },
            ).status_code
            == 201
        )
        ledger = client.get(f"/campaigns/{cid}/ledger").json()
        assert ledger["state"]["status"] == "ready"
        assert (
            len([e for e in ledger["history"] if json.loads(e["event"])["kind"] == "assessed"]) == 2
        )
        assert (
            client.post(url, json=record.model_dump() | {"authority": "runtime"}).status_code == 422
        )


@pytest.mark.parametrize("receipt_kind", ["review_correction", "unknown_record"])
def test_assessment_distinguishes_correction_receipts_without_ignoring_unknown_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt_kind: str
) -> None:
    monkeypatch.setattr(module, "pinned_files", snapshot_fixture)
    harness = module.CampaignHarness(tmp_path)

    def existing_harness(_: Path) -> module.CampaignHarness:
        return harness

    monkeypatch.setattr(module, "CampaignHarness", existing_harness)
    with TestClient(module.create_app(tmp_path)) as client:
        cid = client.post("/campaigns", json=config().model_dump()).json()["id"]
        with harness.store.connect() as db:
            harness.store.append(
                db,
                cid,
                CampaignEvent("assessed"),
                json.dumps({"kind": receipt_kind, "failed_run_id": "historical-run"}),
            )
        response = client.post(f"/campaigns/{cid}/assessments", json=assessment().model_dump())
        assert response.status_code == (201 if receipt_kind == "review_correction" else 409)
        history = client.get(f"/campaigns/{cid}/ledger").json()["history"]
        assessed = [h for h in history if json.loads(h["event"])["kind"] == "assessed"]
        assert len(assessed) == (2 if receipt_kind == "review_correction" else 1)
        assert json.loads(assessed[0]["detail"])["kind"] == receipt_kind


def test_memory_labels_revision_deduplicates_and_cannot_change_authority() -> None:
    old = assessment().model_dump_json()
    current = assessment(revision="b" * 40).model_dump_json()
    items = assessment_context([current, current, old], "b" * 40)
    assert [i["historical"] for i in items] == [False, True]
    assert all("never authority" in str(i["trust"]) for i in items)
    prior = json.dumps(
        {
            "revision": "a" * 40,
            "report": {"summary": "stale report", "findings": [], "unresolved": []},
        }
    )
    assert "stale report" not in module.prior_review_context([prior], revision="b" * 40)
    state = CampaignState(status="parked")
    assert reduce_campaign(state, CampaignEvent("assessed"), task_count=10) == state
    with pytest.raises(ValueError, match="completed"):
        reduce_campaign(CampaignState(status="complete"), CampaignEvent("assessed"), task_count=10)
    with pytest.raises(ValidationError):
        assessment(paths=["../escape.py"])


def test_workload_covers_failure_test_callee_and_dated_continuation() -> None:
    files = {
        "src/service.ts": "import {check} from './guard';\nif (!check()) throw Error('denied');",
        "src/guard.ts": "export function check() { return false; }",
        "src/service.test.ts": "it('denies invalid tenant', () => {});",
        "docs/plan.md": "dated table\n"
        + "old\n" * 64
        + "Historical snapshot superseded\ncurrent\n",
    }
    workload = plan_workload(files, "src/service.ts", "bugs", 1)
    assert set(workload.obligations) == {"src/service.ts", "src/service.test.ts", "src/guard.ts"}
    docs = plan_workload(files, "docs", "docs", 1)
    assert any(start <= 66 <= end for start, end in docs.obligations["docs/plan.md"])
    tools = ReviewTools(files, "")
    tools.required_intervals = workload.obligations
    tools.report = module.ReviewReport(summary="no claims", findings=[], unresolved=[])
    assert not tools.verify(module.RunState(RunId("coverage"))).passed
    for path in workload.obligations:
        assert tools.execute(
            ToolExecutionRequest(ActionProposal(ActionId(path), "read_file", {"path": path}), 1, 5)
        ).ok
    assert tools.verify(module.RunState(RunId("coverage"))).passed
    with pytest.raises(ValueError, match="No reviewable"):
        plan_workload(files, "absent", "bugs", 1)
    assert plan_workload(files, "src/service.test.ts", "bugs", 1).obligations


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


def test_review_unit_aggregate_caps_and_unchanged_failure_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "pinned_files", snapshot_fixture)
    harness = module.CampaignHarness(tmp_path)
    cid = harness.create(config())
    events = SQLiteEventStore(tmp_path / "runs.sqlite", codec=JsonEventCodec())
    task = f"{cid}:s1:0"
    assert harness.review_allowance(cid, task, config(), events, "v1") == config()
    runtime, _, _ = build_review(
        config(), files={}, previous="", store=events, model=ScriptedModel([])
    )
    rid = runtime.start("bounded fixture")
    runtime.cancel(rid, summary="simulated failure")
    with harness.store.connect() as db:
        harness.store.append(db, cid, CampaignEvent("claimed", task), "{}")
        harness.store.append(
            db,
            cid,
            CampaignEvent("linked", task),
            json.dumps({"run_id": str(rid), "implementation": "v1"}),
        )
    with pytest.raises(ValueError, match="corrective change"):
        harness.review_allowance(cid, task, config(), events, "v1")
    with harness.store.connect() as db:
        harness.store.append(
            db,
            cid,
            CampaignEvent("assessed", task),
            json.dumps(
                {"kind": "review_correction", "failed_run_id": str(rid), "implementation": "v2"}
            ),
        )
    assert (
        harness.review_allowance(cid, task, config(), events, "v2").max_tokens
        <= config().max_tokens
    )
    contract = ReviewUnitContract(task_id=task, max_attempts=4, max_tokens=50, max_seconds=20)
    assert contract.remaining(1, 30, 5) == (20, 15)
    for attempts, tokens, seconds in [(4, 0, 0), (0, 50, 0), (0, 0, 20)]:
        with pytest.raises(ValueError, match="exhausted"):
            contract.remaining(attempts, tokens, seconds)


def test_snapshot_cache_poison_is_not_review_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str]] = []

    def pinned(cfg: CampaignConfig) -> dict[str, str]:
        del cfg
        return {"a.py": "PINNED"}

    monkeypatch.setattr(module, "pinned_files", pinned)
    harness = module.CampaignHarness(tmp_path)
    cid = harness.create(config())
    (tmp_path / f"{cid}.snapshot.json").write_text(json.dumps({"a.py": "POISON"}))
    original = module.build_review

    def build(
        cfg: CampaignConfig, *, files: dict[str, str], previous: str, store: StateStorePort
    ) -> tuple[module.Runtime, ReviewTools, OllamaModel | None]:
        seen.append(files)
        return original(cfg, files=files, previous=previous, store=store, model=ScriptedModel([]))

    monkeypatch.setattr(module, "build_review", build)
    harness.acquire_service()
    try:
        harness.tick(cid)
        assert harness.worker is not None
        harness.worker.join(5)
        assert seen == [{"a.py": "PINNED"}]
    finally:
        harness.close()


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


def test_activity_paginates_history_and_denies_cross_campaign_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "pinned_files", snapshot_fixture)
    harness = module.CampaignHarness(tmp_path)
    cid = harness.create(config())
    events = SQLiteEventStore(tmp_path / "runs.sqlite", codec=JsonEventCodec())
    runtime, _, _ = build_review(
        config(), files={}, previous="", store=events, model=ScriptedModel([])
    )
    identities: list[str] = []
    task = f"{cid}:s1:0"
    with harness.store.connect() as db:
        harness.store.append(db, cid, CampaignEvent("claimed", task), "{}")
        for _ in range(3):
            rid = runtime.start("pagination fixture")
            runtime.cancel(rid, summary="preserved evidence")
            identities.append(str(rid))
            harness.store.append(
                db, cid, CampaignEvent("linked", task), json.dumps({"run_id": str(rid)})
            )
    page = module.campaign_activity(tmp_path, harness.store, cid, run_limit=2, event_limit=1)
    assert [r["id"] for r in page["runs"]] == identities[1:]
    assert page["pagination"]["next_run_offset"] == 2
    assert all(len(r["timeline"]) == 1 and r["next_event_offset"] == 1 for r in page["runs"])
    older = module.campaign_activity(tmp_path, harness.store, cid, run_offset=2, run_limit=2)
    assert [r["id"] for r in older["runs"]] == identities[:1]
    exact = module.campaign_activity(tmp_path, harness.store, cid, run_id=identities[0])
    assert [r["id"] for r in exact["runs"]] == identities[:1]
    with pytest.raises(ValueError, match="does not belong"):
        module.campaign_activity(tmp_path, harness.store, cid, run_id="another-campaign-run")
    with pytest.raises(ValueError, match="pagination"):
        module.campaign_activity(tmp_path, harness.store, cid, event_limit=501)


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


def test_followup_overflow_is_durable_revision_pinned_and_has_no_execution_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "pinned_files", snapshot_fixture)
    with TestClient(module.create_app(tmp_path)) as client:
        cid = client.post("/campaigns", json=config().model_dump()).json()["id"]
        store = module.CampaignStore(tmp_path / "campaigns.sqlite")
        with store.connect() as db:
            store.append(
                db,
                cid,
                CampaignEvent("assessed"),
                json.dumps(
                    {
                        "revision": "a" * 40,
                        "overflow_checks": ["Inspect error propagation", "Inspect caller"],
                    }
                ),
            )
        result = client.get(f"/campaigns/{cid}/followups?limit=1").json()
        assert result["total"] == 2
        assert result["next_offset"] == 1
        assert result["items"][0]["revision"] == "a" * 40
        assert "no execution authority" in result["meaning"]
        assert client.get(f"/campaigns/{cid}").json()["state"]["status"] == "ready"
        assert client.get(f"/campaigns/{cid}/followups?limit=101").status_code == 409


def test_n8n_batch_guard_limits_units_and_time_without_removing_child_idempotency() -> None:
    root = Path(__file__).parents[2]
    parent = json.loads((root / "integrations/n8n/campaign-driver.json").read_text())
    child = json.loads((root / "integrations/n8n/bounded-unit.json").read_text())
    node = next(n for n in parent["nodes"] if n["name"] == "Ready For Next Unit")
    expression = node["parameters"]["conditions"]["conditions"][0]["leftValue"][3:-3].strip()
    script = """
const expression = JSON.parse(process.argv[1]);
const cases = [[true, 0, 1000, true], [true, 11, 1799999, true],
               [true, 12, 1000, false], [true, 1, 1800000, false],
               [false, 0, 1000, false]];
for (const [runnable, index, age, expected] of cases) {
  const fn = new Function('$json','$runIndex','$','Date', 'return ('+expression+')');
  const actual = fn({runnable},index,()=>({first:()=>({json:{batchStartedAt:0}})}),{now:()=>age});
  if (actual !== expected) throw Error('batch guard failed');
}
"""
    subprocess.run(["node", "-e", script, json.dumps(expression)], check=True, capture_output=True)
    assert parent["settings"]["executionTimeout"] > child["settings"]["executionTimeout"] > 14400
    claim = next(n for n in child["nodes"] if n["name"] == "Claim Or Reconnect")
    assert "dispatch_key" in claim["parameters"]["jsonBody"]
    assert "dispatchKey" in claim["parameters"]["jsonBody"]
    identity = next(n for n in child["nodes"] if n["name"] == "Unit Identity")
    assert "$execution.id" in identity["parameters"]["jsCode"]


def test_workload_never_overwrites_denial_test_and_resolves_pinned_alias_callee() -> None:
    files = {
        "apps/dashboard/app/_actions.ts": "import { getMembership } from '@/lib/queries';\n"
        "export async function select() { if (!await getMembership()) throw Error(); }",
        "apps/dashboard/app/_actions.test.ts": "import {select} from './_actions';\n"
        + "// setup\n" * 20
        + "it('rejects invalid tenant', () => {});\n"
        + "// later\n" * 30,
        "apps/dashboard/lib/queries.ts": "// helpers\n" * 80
        + "export async function getMembership() { return checkTenant(); }",
    }
    workload = plan_workload(files, "apps/dashboard/app/_actions.ts", "bugs", 1)
    test_ranges = workload.obligations["apps/dashboard/app/_actions.test.ts"]
    assert any(start <= 22 <= end for start, end in test_ranges)
    assert any(
        start <= 81 <= end for start, end in workload.obligations["apps/dashboard/lib/queries.ts"]
    )
    del files["apps/dashboard/lib/queries.ts"]
    without_callee = plan_workload(files, "apps/dashboard/app/_actions.ts", "bugs", 1)
    assert without_callee.obligations["apps/dashboard/app/_actions.test.ts"] == test_ranges
    assert any("boundary is unverified" in message for message in without_callee.limitations)


def test_same_basename_in_another_directory_is_not_a_regression_pair() -> None:
    files = {
        "apps/dashboard/app/account/_actions.ts": "export function updateProfile() {}",
        "apps/dashboard/app/account/_actions.test.ts": (
            "import {updateProfile} from './_actions';\nit('rejects bad profile', () => {});"
        ),
        "apps/dashboard/app/_actions.test.ts": (
            "import {setActiveFirm} from './_actions';\nit('rejects firm', () => {});"
        ),
        "apps/dashboard/app/_actions.ts": "export function setActiveFirm() {}",
    }
    source = "apps/dashboard/app/account/_actions.ts"
    assert matching_tests(files, source) == ["apps/dashboard/app/account/_actions.test.ts"]
    del files["apps/dashboard/app/account/_actions.test.ts"]
    assert matching_tests(files, source) == []
    workload = plan_workload(files, source, "bugs", 1)
    assert "apps/dashboard/app/_actions.test.ts" not in workload.obligations
    assert any("No matching regression" in message for message in workload.limitations)


def test_static_python_imports_pair_real_tests_and_never_match_docstring_mentions() -> None:
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
    assert matching_tests(files, "apps/api/audit_log.py") == ["tests/api/test_audit_log.py"]
    assert matching_tests(files, "apps/api/dependencies/__init__.py") == ["tests/api/test_auth.py"]
    assert import_edges(files, "tests/nodes/test_population.py") == {}
    files["apps/api/consumer.py"] = "from .audit_log import record"
    assert "apps/api/audit_log.py" in import_edges(files, "apps/api/consumer.py")


@pytest.mark.parametrize("initializer", ["", '"""API package."""\n', "from . import _timing\n"])
def test_related_source_prefers_imported_implementation_over_empty_or_reexport_package(
    initializer: str,
) -> None:
    files = {
        "apps/api/__init__.py": initializer,
        "apps/api/_timing.py": "def timed_endpoint():\n    return 200\n",
        "tests/api/test_timing.py": "from apps.api import _timing\n",
    }
    selected = related_source(files, "tests/api/test_timing.py", {})
    assert set(selected) == {"apps/api/_timing.py"}


def test_related_source_retains_package_implementation_and_does_not_invent_edges() -> None:
    files = {
        "apps/api/__init__.py": "def require_key():\n    return False\n",
        "apps/api/unrelated.py": "def require_key():\n    return True\n",
        "tests/api/test_auth.py": "from apps.api import require_key\n",
    }
    assert set(related_source(files, "tests/api/test_auth.py", {})) == {"apps/api/__init__.py"}
    assert related_source(files, "tests/api/test_auth.py", {"apps/api/__init__.py": [(1, 2)]}) == {}


@pytest.mark.parametrize("definition", ["def target():", "async def target():", "class target:"])
def test_python_definition_anchor_ignores_documentation_and_locates_real_symbols(
    definition: str,
) -> None:
    text = "\n".join(
        [
            '"""def target():',
            "except described_error:",
            '"""',
            "# def target():",
            definition,
            "    pass",
        ]
    )
    assert definition_anchor("impl.py", text, ["target"]) == 5
    assert definition_anchor("impl.py", text, ["absent"]) == 0
    assert definition_anchor("impl.py", "def broken(:", ["broken"]) == 0


def test_python_related_source_fallback_uses_executable_branch_after_docstring() -> None:
    text = "\n".join(
        [
            '"""except description:',
            "def misleading():",
            '"""',
            *["# padding"] * 25,
            "def real():",
            "    return 200",
        ]
    )
    files = {"impl.py": text, "consumer.py": "import impl"}
    interval = related_source(files, "consumer.py", {})["impl.py"][0]
    assert interval[0] > 3
    assert interval[0] <= 30 <= interval[1]
    assert interval[1] - interval[0] + 1 <= 18


def test_docs_lane_prefers_current_document_and_marks_code_only_scope_unverified() -> None:
    files = {
        "apps/api/contract.md": "# Current API contract\nCalls reject missing keys.\n",
        "apps/api/handler.py": "def handler():\n    raise ValueError()\n",
    }
    with_doc = plan_workload(files, "apps/api", "docs", 1)
    assert "apps/api/contract.md" in with_doc.obligations
    assert "No documentation claim selected" not in " ".join(with_doc.limitations)
    without_doc = plan_workload(
        {"apps/api/handler.py": files["apps/api/handler.py"]}, "apps/api", "docs", 1
    )
    assert "apps/api/handler.py" in without_doc.obligations
    assert any("documentation drift is unverified" in s for s in without_doc.limitations)
    assert "not documentation-drift evidence" in without_doc.objective


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


def test_typescript_import_anchor_skips_comment_example_and_blank_lines() -> None:
    files = {
        "src/main.ts": (
            '/*\nimport { run } from "./worker";\n*/\n\nimport { run as local } from "./worker";\n'
        ),
        "src/worker.ts": "export const run = () => 1;\n",
    }
    assert import_anchor(files, "src/main.ts", "src/worker.ts") == 5


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


def test_unknown_usage_excludes_only_known_pre_request_stops() -> None:
    assert not unknown_provider_usage("CODING_NO_NEW_EVIDENCE", False)
    assert not unknown_provider_usage("REVIEW_NO_NEW_EVIDENCE", False)
    assert not unknown_provider_usage("MODEL_CONTEXT_LIMIT", False)
    assert unknown_provider_usage("REPORT_USAGE_UNKNOWN", False)
    assert not unknown_provider_usage("MODEL_CONTEXT_LIMIT", True)


def test_wide_scope_prefers_executable_source_and_tests_start_at_actual_cases() -> None:
    files = {
        "apps/workers/__init__.py": '"""Worker entrypoints and job modules."""',
        "apps/workers/worker.py": "def run():\n    raise ValueError('failure')",
        "tests/test_worker.py": "# error-related setup\n" * 40
        + "from apps.workers.worker import run\n\ndef test_failure():\n    run()",
    }
    workload = plan_workload(files, "apps/workers", "map", 1)
    assert "apps/workers/worker.py" in workload.obligations
    assert "apps/workers/__init__.py" not in workload.obligations
    assert workload.obligations["tests/test_worker.py"][0][0] >= 40
    assert case_interval("# error in setup\nfrom pytest import raises") is None
    assert case_interval("def test_happy():\n    assert True") is not None


def test_python_evidence_anchors_execute_code_and_actual_caller_import() -> None:
    source = '"""failure return 503 describes intent, not execution"""\n' + "# header\n" * 30
    source += (
        "def run():\n    stats = invoke()\n"
        "    stats['success'] = not stats['errors']\n    return stats\n"
    )
    caller = "def dispatch():\n" + "    # other dispatches\n" * 60
    caller += "    from apps.workers.job import run\n    return run\n"
    files = {"apps/workers/job.py": source, "scripts/cron.py": caller}
    w = plan_workload(files, "apps/workers/job.py", "bugs", 1)
    assert w.obligations["apps/workers/job.py"][0][0] > 25
    assert w.obligations["scripts/cron.py"][0][0] > 55
    assert any(start <= 34 <= end for start, end in w.obligations["apps/workers/job.py"])
    del files["scripts/cron.py"]
    without_caller = plan_workload(files, "apps/workers/job.py", "bugs", 1)
    assert len(without_caller.obligations) == 1
    assert any("boundary is unverified" in message for message in without_caller.limitations)


def test_explicit_test_directory_selects_cases_instead_of_only_shared_fixtures() -> None:
    files = {
        "tests/conftest.py": "def fixture():\n    return 1",
        "tests/test_worker.py": "def helper():\n    return 1\n"
        + "# setup\n" * 40
        + "def test_denied():\n    assert not permitted()",
        "apps/worker.py": "def permitted():\n    return False",
    }
    w = plan_workload(files, "tests", "bugs", 1)
    assert "tests/test_worker.py" in w.obligations
    assert "tests/conftest.py" not in w.obligations
    assert w.obligations["tests/test_worker.py"][0][0] > 35
    explicit = plan_workload(files, "tests/conftest.py", "bugs", 1)
    assert "tests/conftest.py" in explicit.obligations
    no_cases = dict(files)
    del no_cases["tests/test_worker.py"]
    assert "tests/conftest.py" in plan_workload(no_cases, "tests", "bugs", 1).obligations
    broad = plan_workload(files, "apps", "bugs", 1)
    assert "apps/worker.py" in broad.obligations
    assert "tests/test_worker.py" not in broad.obligations


def test_module_anchor_avoids_headers_and_preserves_explicit_metadata_focus() -> None:
    text = '"""module failure guard documentation"""\n' + "# header\n" * 40
    text += "from dataclasses import dataclass\n\n@dataclass\nclass Config:\n    size: int = 1\n"
    source = "pkg/config.py"
    w = plan_workload({source: text}, source, "bugs", 1)
    assert w.obligations[source][0][0] > 35
    assert any(start <= 45 <= end for start, end in w.obligations[source])
    module = '"""module-only description"""'
    doc = plan_workload({source: module}, source, "bugs", 1)
    assert doc.obligations[source] == [(1, 1)]
    assert any("boundary is unverified" in message for message in doc.limitations)


def test_document_lanes_rotate_sections_without_reading_other_sections_or_claiming_live_state() -> (
    None
):
    text = "# Overview\nCurrent description.\n\n## Failure handling\nDocumented error contract.\n\n"
    text += "## Validation\nRun documented tests.\n"
    files = {"README.md": text}
    first = plan_workload(files, "README.md", "map", 1)
    second = plan_workload(files, "README.md", "bugs", 1)
    assert first.obligations["README.md"] == [(1, 3)]
    assert second.obligations["README.md"] == [(4, 6)]
    assert all(end < 7 for _, end in second.obligations["README.md"])
    assert any("live state are unverified" in item for item in second.limitations)
    tiny = plan_workload({"README.md": "one line"}, "README.md", "bugs", 2)
    assert tiny.obligations["README.md"] == [(1, 1)]


def test_bounded_worker_preserves_actual_angle_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "pinned_files", snapshot_fixture)
    objectives: list[str] = []
    original = module.build_review

    def build(
        cfg: CampaignConfig, *, files: dict[str, str], previous: str, store: StateStorePort
    ) -> tuple[module.Runtime, ReviewTools, OllamaModel | None]:
        runtime, tools, live = original(
            cfg, files=files, previous=previous, store=store, model=ScriptedModel([])
        )
        return runtime, tools, live

    start = module.Runtime.start

    def record(
        runtime: module.Runtime, objective: str, *, parent_run_id: RunId | None = None
    ) -> RunId:
        objectives.append(objective)
        return start(runtime, objective, parent_run_id=parent_run_id)

    monkeypatch.setattr(module.Runtime, "start", record)
    monkeypatch.setattr(module, "build_review", build)
    harness = module.CampaignHarness(tmp_path)
    harness.acquire_service()
    try:
        cid = harness.create(config())
        harness.tick(cid)
        assert harness.worker is not None
        harness.worker.join(5)
        assert len(objectives) == 1
        assert "THIS UNIT" not in objectives[0]  # The bounded rewrite actually ran.
        assert module.LANE_GUIDANCE["map"] in objectives[0]
        assert module.LANE_GUIDANCE["bugs"] not in objectives[0]
    finally:
        harness.close()


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
