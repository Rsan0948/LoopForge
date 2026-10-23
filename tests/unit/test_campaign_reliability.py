"""Independent regressions for audit failures; no provider/network access."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.scripted import FixedClock, ScriptedModel
from loopforge.domain.actions import ActionProposal
from loopforge.domain.campaign import CampaignEvent
from loopforge.domain.coding import CodingEvent, CodingState, reduce_coding
from loopforge.domain.context import ContextItem, ContextSource, ModelContext, ModelRole
from loopforge.domain.events import BudgetDebited
from loopforge.domain.issues import IssueEvent, IssueState, reduce_issue
from loopforge.domain.security import TrustClass
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, ContextItemId, RunId, RunStatus, UsageDelta
from loopforge.domain.verification import CheckOutcome
from loopforge.entrypoints import campaign as campaign_module
from loopforge.entrypoints import campaign_review as review_module
from loopforge.entrypoints.campaign import CampaignHarness
from loopforge.entrypoints.campaign_evidence import (
    candidate_snapshot,
    missing_coverage,
    required_reads,
)
from loopforge.entrypoints.campaign_execution import PatchReviewVerifier
from loopforge.entrypoints.campaign_issues import IssueCommand, IssueConfig, Obligation
from loopforge.entrypoints.campaign_models import CampaignConfig, ReviewReport
from loopforge.entrypoints.campaign_review import (
    REVIEW_TEMPLATE,
    ReportingReviewModel,
    ReviewTools,
    build_review,
    tool_specs,
)
from loopforge.ports.model import ModelFailureClass, ModelPort, ModelTurn, ModelTurnError
from loopforge.ports.reporting import ReportTurn
from loopforge.ports.state_store import StateStorePort
from loopforge.ports.tools import ToolExecutionRequest, ToolResult
from loopforge.ports.verifier import VerificationResult


def call(tools: ReviewTools, name: str, **args: str) -> ToolResult:
    return tools.execute(ToolExecutionRequest(ActionProposal(ActionId("probe"), name, args), 1, 5))


def test_reporting_packet_deduplicates_overlap_without_inventing_or_retargeting() -> None:
    tools = ReviewTools({}, "")
    original = tools.register_excerpt("a.py", 10, "ten\neleven\ntwelve")
    tools.register_excerpt("a.py", 11, "eleven\ntwelve\nthirteen")
    tools.register_excerpt("a.py", 20, "twenty")
    tools.register_excerpt("b.py", 10, "different")
    before = dict(tools.references)
    packet = tools.reporting_observations()
    lines: dict[tuple[str, int], str] = {}
    for item in packet:
        path, start, text = str(item["path"]), int(item["start_line"]), str(item["text"])
        assert any(path == p and start >= s and text in t for p, s, t in before.values())
        assert tools.references[str(item["excerpt_id"])] == (path, start, text)
        for number, line in enumerate(text.split("\n"), start):
            assert (path, number) not in lines
            lines[(path, number)] = line
    assert lines == {
        ("a.py", 10): "ten",
        ("a.py", 11): "eleven",
        ("a.py", 12): "twelve",
        ("a.py", 13): "thirteen",
        ("a.py", 20): "twenty",
        ("b.py", 10): "different",
    }
    assert tools.references[original] == before[original]
    assert tools.reporting_observations() == packet


def test_reporting_packet_never_fills_unobserved_gaps() -> None:
    tools = ReviewTools({"a.py": "first\nUNREAD\nthird"}, "")
    tools.register_excerpt("a.py", 1, "first")
    tools.register_excerpt("a.py", 3, "third")
    assert "UNREAD" not in json.dumps(tools.reporting_observations())


def report() -> str:
    return json.dumps({"summary": "bounded fixture", "findings": [], "unresolved": []})


class PassingChecks:
    def verify(self, state: RunState) -> VerificationResult:
        del state
        return VerificationResult(True, "fixture checks pass", 1)


def test_clean_report_requires_actual_source() -> None:
    tools = ReviewTools({"a.py": "actual()"}, "")
    assert call(tools, "submit_report", report=report()).ok
    assert not tools.verify(RunState(RunId("fixture"))).passed
    assert call(tools, "read_file", path="a.py").ok
    assert tools.verify(RunState(RunId("fixture"))).passed


def test_nonadjacent_quote_cannot_be_manufactured() -> None:
    tools = ReviewTools({"a.py": "x" * 2990 + "ALPHA\nHIDDEN\nOMEGA"}, "")
    assert call(tools, "read_file", path="a.py").ok
    assert call(tools, "read_file", path="a.py", start_line="3").ok
    data = {
        "summary": "fixture",
        "findings": [
            {
                "title": "probe",
                "severity": "low",
                "disposition": "uncertain",
                "explanation": "fixture",
                "next_check": "inspect",
                "evidence": [{"path": "a.py", "quote": "ALPHA\nOMEGA"}],
            }
        ],
        "unresolved": [],
    }
    assert not call(tools, "submit_report", report=json.dumps(data)).ok
    assert call(
        tools, "submit_report", report=json.dumps(data).replace("ALPHA\\nOMEGA", "OMEGA")
    ).ok


def test_header_only_review_fails_then_actual_intervals_pass() -> None:
    files = {
        "a.py": "header\n" * 300 + "CHANGED\n",
        "CAMPAIGN_PATCH.diff": "header\n" * 300 + "ACTUAL HUNK\n",
    }
    tools = ReviewTools(files, "")
    for path in files:
        assert call(tools, "read_file", path=path).ok
    assert call(tools, "submit_report", report=report()).ok
    verifier = PatchReviewVerifier(tools, PassingChecks(), set(files))
    assert not verifier.verify(RunState(RunId("fixture"))).passed
    for path in files:
        assert call(tools, "read_file", path=path, start_line="201").ok
    assert verifier.verify(RunState(RunId("fixture"))).passed


def test_candidate_snapshot_preserves_deletion_and_excludes_secret_files(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@example.invalid")
    (tmp_path / "gone.py").write_text("deleted_source()\n")
    (tmp_path / ".env.local").write_text("FAKE_SECRET=fixture\n")
    (tmp_path / "private.key").write_text("FAKE_KEY\n")
    git("add", ".")
    git("commit", "-qm", "fixture")
    workspace = GitWorkspaceManager(tmp_path.parent).adopt_existing(tmp_path)
    (tmp_path / "gone.py").unlink()
    files = candidate_snapshot(workspace)
    assert "deleted_source()" in files["gone.py"]
    assert ".env.local" not in files
    assert "private.key" not in files
    (tmp_path / "private.key").write_text("CHANGED_FAKE_KEY\n")
    with pytest.raises(ValueError, match="excluded source"):
        candidate_snapshot(workspace)


def test_interval_coverage_keeps_gaps_and_merges_adjacent_reads() -> None:
    required = {"a.py": [(1, 3)]}
    assert missing_coverage({("a.py", 1): "a", ("a.py", 3): "c"}, required)
    assert not missing_coverage({("a.py", 1): "a\nb", ("a.py", 3): "c"}, required)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "tool_calls": []},
        {"role": "assistant", "tool_calls": "invalid"},
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": 42}}}],
        },
    ],
)
def test_invalid_provider_generation_is_debited_and_budget_stops_retry(
    message: dict[str, object],
) -> None:
    cfg = CampaignConfig(
        repository="/fixture", project="brook", revision="a" * 40, scope=["."], max_tokens=1000
    )
    store = InMemoryEventStore()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "message": message,
                "done": True,
                "prompt_eval_count": 1000,
                "eval_count": 100,
            },
        )

    model = OllamaModel(
        model=cfg.model,
        tools=tool_specs(),
        template=REVIEW_TEMPLATE,
        transport=httpx.MockTransport(handler),
    )
    runtime, _, _ = build_review(cfg, files={"a.py": "code"}, previous="", store=store, model=model)
    try:
        state = runtime.run("fixture")
    finally:
        model.close()
    assert state.status == RunStatus.BUDGET_EXHAUSTED
    assert state.total_tokens == 1100
    assert len(requests) == 1
    assert len([e for e in store.events_for(state.run_id) if isinstance(e, BudgetDebited)]) == 1


def test_stepping_does_not_repeat_verification() -> None:
    cfg = CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])
    actions = [
        ActionProposal(ActionId("read"), "read_file", {"path": "a.py"}),
        ActionProposal(ActionId("report"), "submit_report", {"report": report()}),
    ]
    runtime, tools, _ = build_review(
        cfg,
        files={"a.py": "code"},
        previous="",
        store=InMemoryEventStore(),
        model=ScriptedModel(actions),
    )
    count = 0

    class Counter:
        def verify(self, state: RunState) -> VerificationResult:
            nonlocal count
            count += 1
            return tools.verify(state)

    runtime.verifier = Counter()
    run = runtime.start("fixture")
    while not runtime.state_for(run).status.is_terminal:
        runtime.step(run)
    assert runtime.state_for(run).status == RunStatus.SUCCEEDED
    assert count == 2


def test_inconclusive_or_unexecuted_checks_cannot_pass() -> None:
    with pytest.raises(ValueError, match="inconclusive"):
        VerificationResult(True, "bad fixture", inconclusive=True)
    with pytest.raises(ValueError, match="unexecuted"):
        CheckOutcome(name="fixture", passed=True, detail="never ran", executed=False)


def test_issue_terminal_and_attempt_limits() -> None:
    def apply(state: IssueState, event: IssueEvent) -> IssueState:
        return reduce_issue(state, event, max_attempts=1, max_tokens=100)

    state = apply(IssueState(), IssueEvent("start"))
    state = apply(state, IssueEvent("failed", tokens=20))
    with pytest.raises(ValueError, match="exhausted"):
        apply(state, IssueEvent("retry"))
    state = apply(state, IssueEvent("reject"))
    with pytest.raises(ValueError, match="terminal"):
        apply(state, IssueEvent("start"))


@pytest.mark.parametrize(
    "event", [IssueEvent("failed", tokens=100), IssueEvent("failed", seconds=30)]
)
def test_issue_aggregate_consumption_denies_retry(event: IssueEvent) -> None:
    state = reduce_issue(
        IssueState(), IssueEvent("start"), max_attempts=5, max_tokens=100, max_seconds=30
    )
    state = reduce_issue(state, event, max_attempts=5, max_tokens=100, max_seconds=30)
    with pytest.raises(ValueError, match="exhausted"):
        reduce_issue(state, IssueEvent("retry"), max_attempts=5, max_tokens=100, max_seconds=30)


def test_required_read_plan_rejects_unreadable_line_and_counts_pages() -> None:
    assert required_reads({"a": "x\n" * 401}, {"a": [(1, 401)]}) == 3
    assert required_reads({"a": "x" * 3001}, {"a": [(1, 1)]}) == 2


@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("at_checkpoint", [False, True])
def test_issue_dispatch_preserves_cursor_and_durable_results(  # noqa: PLR0915 - full durable lifecycle
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject: bool, at_checkpoint: bool
) -> None:

    cfg = CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])

    def pinned_fixture(_cfg: CampaignConfig) -> dict[str, str]:
        return {"a.py": "observed()"}

    monkeypatch.setattr(campaign_module, "pinned_files", pinned_fixture)
    original_build = build_review

    def fixture_build(  # noqa: PLR0913 - mirrors the composition seam
        cfg: CampaignConfig,
        *,
        files: dict[str, str],
        previous: str,
        store: StateStorePort,
        model: ModelPort | None = None,
        patch_review: bool = False,
        max_source_reads: int = 10,
    ):
        del model, patch_review
        actions = [
            ActionProposal(ActionId("read"), "read_file", {"path": "a.py"}),
            ActionProposal(ActionId("report"), "submit_report", {"report": report()}),
        ]
        return original_build(
            cfg,
            files=files,
            previous=previous,
            store=store,
            model=ScriptedModel(actions),
            max_source_reads=max_source_reads,
        )

    monkeypatch.setattr(campaign_module, "build_review", fixture_build)
    harness = CampaignHarness(tmp_path)
    harness.acquire_service()
    try:
        campaign = harness.create(cfg)
        cursor = cfg.rounds * len(cfg.scope) * 7 if at_checkpoint else 0
        if at_checkpoint:
            with harness.store.connect() as db:
                for index in range(cursor):
                    task = f"{campaign}:s1:{index}"
                    harness.store.append(db, campaign, CampaignEvent("claimed", task), "{}")
                    harness.store.append(db, campaign, CampaignEvent("finished", task), "{}")
            assert not harness.snapshot(campaign)["runnable"]
            assert not harness.tick(campaign)["accepted"]
        issue = harness.enqueue_issue(
            campaign,
            IssueConfig(
                objective="Verify a narrow implementation claim",
                evidence="fixture evidence basis",
                obligations=[Obligation(path="a.py", start_line=1, end_line=1)],
            ),
        )
        assert harness.snapshot(campaign)["runnable"]
        assert harness.tick(campaign)["accepted"]
        assert harness.worker is not None
        harness.worker.join(timeout=5)
        assert not harness.worker.is_alive()
        snapshot = harness.snapshot(campaign)
        with harness.store.connect() as db:
            state = harness.issues.state(db, issue)
            assert state.status == "investigated"
            assert harness.store.state(db, campaign).cursor == cursor
        assert not snapshot["runnable"]
        assert harness.tick(campaign).get("reason") == "issue_checkpoint"
        assert snapshot["slot"] is None
        restored = CampaignHarness(tmp_path)
        with restored.store.connect() as db:
            assert restored.issues.state(db, issue) == state
        harness.issue_command(
            campaign,
            issue,
            IssueCommand(
                command="reject" if reject else "close",
                evidence="operator examined fixture evidence",
            ),
        )
        assert harness.snapshot(campaign)["runnable"] == (not at_checkpoint)
        if at_checkpoint:
            assert not harness.tick(campaign)["accepted"]
            with harness.store.connect() as db:
                assert harness.store.state(db, campaign).cursor == cursor
                assert harness.store.state(db, campaign).status == "checkpoint"
        else:
            assert harness.tick(campaign)["accepted"]
            assert harness.worker is not None
            harness.worker.join(timeout=5)
            assert not harness.worker.is_alive()
            with harness.store.connect() as db:
                assert harness.store.state(db, campaign).cursor == 1
        with pytest.raises(ValueError, match="terminal"):
            harness.issue_command(
                campaign,
                issue,
                IssueCommand(command="retry", evidence="cannot resurrect this closed issue"),
            )
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("trust", "budget", "fail_once"),
    [
        (TrustClass.AUTHORIZED_HUMAN, 100_000, False),
        (TrustClass.AUTHORIZED_HUMAN, 100_000, True),
        (TrustClass.UNTRUSTED_CONTENT, 100_000, False),
        (TrustClass.AUTHORIZED_HUMAN, 1000, False),
    ],
)
def test_report_finalization_uses_native_schema_and_validates_references(
    monkeypatch: pytest.MonkeyPatch,
    trust: TrustClass,
    budget: int,
    fail_once: bool,
) -> None:

    cfg = CampaignConfig(
        repository="/fixture", project="brook", revision="a" * 40, scope=["."], max_tokens=budget
    )
    tools = ReviewTools({"a.py": "observed()"}, "")
    assert call(tools, "read_file", path="a.py").ok
    captured: list[str] = []

    class Reporter:
        def __init__(self, model: str, *, timeout_seconds: float) -> None:
            assert model == cfg.model
            assert timeout_seconds > 0

        def generate(
            self, packet: str, schema: dict[str, object], *, objective: str = ""
        ) -> ReportTurn:
            assert objective == (
                "Check authorized fixture" if trust is TrustClass.AUTHORIZED_HUMAN else ""
            )
            captured.append(packet)
            assert schema["additionalProperties"] is False
            content = "{invalid" if fail_once and len(captured) == 1 else report()
            return ReportTurn(content, UsageDelta(input_tokens=20, output_tokens=10))

        def close(self) -> None:
            pass

    monkeypatch.setattr(review_module, "OllamaReporting", Reporter)
    selected = ScriptedModel([ActionProposal(ActionId("finish"), "finalize_report", {})])
    model = ReportingReviewModel(selected, tools, cfg, InMemoryEventStore())
    clock = FixedClock(datetime(2026, 10, 10, tzinfo=UTC))
    context = ModelContext(
        run_id=RunId("fixture"),
        items=(
            ContextItem(
                item_id=ContextItemId("fixture:objective"),
                content="Check authorized fixture",
                trust=trust,
                source=ContextSource(origin=trust, reference="fixture"),
                created_at=clock.now(),
            ),
        ),
        assembled_at=clock.now(),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    if budget == 1000:
        with pytest.raises(ModelTurnError, match="Remaining immutable") as caught:
            model.propose_action(context)
        assert caught.value.usage is not None
        assert caught.value.usage.input_tokens == 100
        assert not captured
        return
    if fail_once:
        with pytest.raises(ModelTurnError) as caught:
            model.propose_action(context)
        assert caught.value.usage is not None
        assert caught.value.usage.input_tokens == 120
    turn = model.propose_action(context)
    assert turn.action.tool_name == "submit_report"
    assert turn.usage.input_tokens == (20 if fail_once else 120)
    if fail_once:
        assert "validation_error" in captured[1]
        assert "{invalid" in captured[1]
    assert "observed()" in captured[0]
    assert call(tools, "submit_report", **turn.action.arguments).ok


@pytest.mark.parametrize("case", ["covered", "missing", "unspecified", "other_error", "billed"])
def test_full_controller_context_only_reports_with_mandatory_evidence(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    tools = ReviewTools({"a.py": "observed()\nunread()"}, "")
    assert call(tools, "read_file", path="a.py").ok
    if case != "unspecified":
        tools.required_intervals = {"a.py": [(1, 3 if case == "missing" else 2)]}
    reason = "MODEL_UNAVAILABLE" if case == "other_error" else "MODEL_CONTEXT_LIMIT"

    class FullModel(ScriptedModel):
        def propose_action(self, context: ModelContext) -> ModelTurn:
            del context
            raise ModelTurnError(
                ModelFailureClass.PERMANENT,
                reason,
                "fixture context limit",
                usage=UsageDelta(input_tokens=10) if case == "billed" else None,
            )

    packets: list[str] = []

    class Reporter:
        def __init__(self, model: str, *, timeout_seconds: float) -> None:
            del model, timeout_seconds

        def generate(
            self, packet: str, schema: dict[str, object], *, objective: str = ""
        ) -> ReportTurn:
            del schema, objective
            packets.append(packet)
            return ReportTurn(report(), UsageDelta(input_tokens=20, output_tokens=10))

        def close(self) -> None:
            pass

    monkeypatch.setattr(review_module, "OllamaReporting", Reporter)
    cfg = CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])
    model = ReportingReviewModel(FullModel([]), tools, cfg, InMemoryEventStore())
    now = datetime(2026, 10, 10, tzinfo=UTC)
    context = ModelContext(
        run_id=RunId("context-fixture"),
        items=(
            ContextItem(
                item_id=ContextItemId("fixture:observation"),
                content="duplicate source observation",
                trust=TrustClass.UNTRUSTED_CONTENT,
                source=ContextSource(origin=TrustClass.UNTRUSTED_CONTENT, reference="fixture"),
                created_at=now,
            ),
        ),
        assembled_at=now,
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    if case != "covered":
        with pytest.raises(ModelTurnError) as caught:
            model.propose_action(context)
        assert caught.value.reason_code == (
            "REVIEW_REQUIRED_EVIDENCE_INVALID" if case == "missing" else reason
        )
        assert not packets
        return
    turn = model.propose_action(context)
    assert turn.action.tool_name == "submit_report"
    assert turn.usage == UsageDelta(input_tokens=20, output_tokens=10)
    assert "duplicate source observation" not in packets[0]
    assert "observed()" in packets[0]
    assert json.loads(packets[0])["required_intervals"] == {"a.py": [[1, 2]]}
    assert call(tools, "submit_report", **turn.action.arguments).ok
    assert tools.verify(RunState(RunId("context-fixture"))).passed


def test_covered_review_can_read_missing_consumer_before_reporting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = ReviewTools(
        {"impl.py": "return 1", "consumer.py": "assert impl() == 2"}, "", max_source_reads=3
    )
    tools.required_intervals = {"impl.py": [(1, 1)]}
    assert call(tools, "read_file", path="impl.py").ok
    packets: list[str] = []

    class Reporter:
        def __init__(self, model: str, *, timeout_seconds: float) -> None:
            del model, timeout_seconds

        def generate(
            self, packet: str, schema: dict[str, object], *, objective: str = ""
        ) -> ReportTurn:
            del schema, objective
            packets.append(packet)
            return ReportTurn(report(), UsageDelta())

        def close(self) -> None:
            pass

    monkeypatch.setattr(review_module, "OllamaReporting", Reporter)
    selected = ScriptedModel(
        [
            ActionProposal(ActionId("consumer"), "read_file", {"path": "consumer.py"}),
            ActionProposal(ActionId("finish"), "finalize_report", {}),
        ]
    )
    cfg = CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])
    model = ReportingReviewModel(selected, tools, cfg, InMemoryEventStore())
    context = ModelContext(
        run_id=RunId("consumer-fixture"),
        items=(),
        assembled_at=datetime(2026, 10, 10, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    extra = model.propose_action(context)
    assert extra.action.tool_name == "read_file"
    assert extra.action.arguments == {"path": "consumer.py"}
    assert not packets
    assert tools.execute(ToolExecutionRequest(extra.action, 1, 5)).ok
    submitted = model.propose_action(context)
    assert submitted.action.tool_name == "submit_report"
    assert "assert impl() == 2" in packets[0]


def test_patch_review_rejects_repeated_report_without_new_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = ReviewTools(
        {"impl.py": "return 1", "consumer.py": "assert impl() == 2"}, "", max_source_reads=3
    )
    tools.required_intervals = {"impl.py": [(1, 1)]}
    tools.require_new_evidence_after_report = True
    assert call(tools, "read_file", path="impl.py").ok
    packets: list[str] = []

    class Reporter:
        def __init__(self, model: str, *, timeout_seconds: float) -> None:
            del model, timeout_seconds

        def generate(
            self, packet: str, schema: dict[str, object], *, objective: str = ""
        ) -> ReportTurn:
            del schema, objective
            packets.append(packet)
            return ReportTurn(report(), UsageDelta())

        def close(self) -> None:
            pass

    monkeypatch.setattr(review_module, "OllamaReporting", Reporter)
    selected = ScriptedModel(
        [ActionProposal(ActionId(f"finish-{i}"), "finalize_report", {}) for i in range(3)]
    )
    cfg = CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])
    model = ReportingReviewModel(selected, tools, cfg, InMemoryEventStore())
    context = ModelContext(
        run_id=RunId("patch-fixture"),
        items=(),
        assembled_at=datetime(2026, 10, 10, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    first = model.propose_action(context)
    assert first.action.tool_name == "submit_report"
    assert tools.execute(ToolExecutionRequest(first.action, 1, 5)).ok
    with pytest.raises(ModelTurnError) as caught:
        model.propose_action(context)
    assert caught.value.reason_code == "REVIEW_NO_NEW_EVIDENCE"
    assert len(packets) == 1
    assert call(tools, "read_file", path="consumer.py").ok
    again = model.propose_action(context)
    assert again.action.tool_name == "submit_report"
    assert len(packets) == 2


@pytest.mark.parametrize("case", ["covered", "missing", "reset", "unspecified"])
def test_cached_replay_loop_finalizes_or_stops_without_inventing_coverage(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    tools = ReviewTools({"a.py": "observed()", "b.py": "new_evidence()"}, "")
    assert call(tools, "read_file", path="a.py").ok
    tools.required_intervals = {"a.py": [(1, 2 if case == "missing" else 1)]}
    if case == "unspecified":
        tools.required_intervals = {}
    captured: list[str] = []

    class Reporter:
        def __init__(self, model: str, *, timeout_seconds: float) -> None:
            del model, timeout_seconds

        def generate(
            self, packet: str, schema: dict[str, object], *, objective: str = ""
        ) -> ReportTurn:
            del schema, objective
            captured.append(packet)
            return ReportTurn(report(), UsageDelta(input_tokens=20, output_tokens=10))

        def close(self) -> None:
            pass

    monkeypatch.setattr(review_module, "OllamaReporting", Reporter)
    selected = ScriptedModel(
        [
            ActionProposal(ActionId(f"repeat-{i}"), "observed_excerpt", {"path": "a.py"})
            for i in range(10)
        ]
    )
    cfg = CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])
    model = ReportingReviewModel(selected, tools, cfg, InMemoryEventStore())
    now = datetime(2026, 10, 10, tzinfo=UTC)
    context = ModelContext(
        run_id=RunId("replay-fixture"),
        items=(),
        assembled_at=now,
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    if case == "missing":
        with pytest.raises(ModelTurnError) as caught:
            model.propose_action(context)
        assert caught.value.reason_code == "REVIEW_REQUIRED_EVIDENCE_INVALID"
        assert not captured
        return
    for _ in range(6):
        assert model.propose_action(context).action.tool_name == "observed_excerpt"
    assert not captured
    if case == "reset":
        assert call(tools, "read_file", path="b.py").ok
        assert model.propose_action(context).action.tool_name == "observed_excerpt"
        assert not captured
    elif case in {"missing", "unspecified"}:
        with pytest.raises(ModelTurnError) as caught:
            model.propose_action(context)
        assert caught.value.reason_code == "REVIEW_NO_NEW_EVIDENCE"
        assert not captured
    else:
        assert model.propose_action(context).action.tool_name == "submit_report"
        assert len(captured) == 1
        assert "observed()" in captured[0]


@pytest.mark.parametrize(
    "case",
    [
        "sample",
        "mandatory_missing",
        "unspent",
        "no_evidence",
        "bounded_covered",
        "bounded_missing",
        "bounded_unspecified",
        "ordinary_covered",
        "mandatory_cap",
    ],
)
def test_exhausted_source_sample_finalizes_without_bypassing_mandatory_coverage(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    tools = ReviewTools({"a.py": "observed()"}, "", max_source_reads=1)
    if case != "no_evidence":
        assert call(tools, "read_file", path="a.py").ok
    if case == "mandatory_missing":
        tools.required_intervals = {"a.py": [(1, 2)]}
    if case == "mandatory_cap":
        tools.required_intervals = {"a.py": [(1, 1)]}
    if case == "unspent":
        tools.max_source_reads = 2
    if case.startswith("bounded") or case == "ordinary_covered":
        tools.max_source_reads = 5
        tools.finalize_when_covered = case.startswith("bounded")
        if case != "bounded_unspecified":
            tools.required_intervals = {"a.py": [(1, 2 if case == "bounded_missing" else 1)]}
    captured: list[str] = []

    class Reporter:
        def __init__(self, model: str, *, timeout_seconds: float) -> None:
            del model, timeout_seconds

        def generate(
            self, packet: str, schema: dict[str, object], *, objective: str = ""
        ) -> ReportTurn:
            del schema, objective
            captured.append(packet)
            return ReportTurn(report(), UsageDelta(input_tokens=20, output_tokens=10))

        def close(self) -> None:
            pass

    monkeypatch.setattr(review_module, "OllamaReporting", Reporter)
    selected = ScriptedModel([ActionProposal(ActionId("controller"), "list_files", {})])
    cfg = CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])
    model = ReportingReviewModel(selected, tools, cfg, InMemoryEventStore())
    context = ModelContext(
        run_id=RunId("sample-fixture"),
        items=(),
        assembled_at=datetime(2026, 10, 10, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    if case in {"mandatory_missing", "bounded_missing"}:
        with pytest.raises(ModelTurnError) as caught:
            model.propose_action(context)
        assert caught.value.reason_code == "REVIEW_REQUIRED_EVIDENCE_INVALID"
        assert not captured
        return
    turn = model.propose_action(context)
    if case not in {"sample", "bounded_covered", "mandatory_cap"}:
        assert turn.action.tool_name == "list_files"
        assert not captured
        return
    assert turn.action.tool_name == "submit_report"
    assert turn.usage == UsageDelta(input_tokens=20, output_tokens=10)
    packet = json.loads(captured[0])
    assert packet["coverage_contract"]["kind"] == (
        "sample" if case == "sample" else "mandatory_intervals"
    )
    assert packet["coverage_contract"]["source_reads"] == 1
    assert packet["coverage_contract"]["max_source_reads"] == tools.max_source_reads
    assert "observed()" in captured[0]
    assert call(tools, "submit_report", **turn.action.arguments).ok
    assert tools.report is not None
    assert tools.report.summary.startswith("Bounded source sample;") is (case == "sample")
    assert tools.verify(RunState(RunId("sample-fixture"))).passed
    assert tools.source_reads == 1


def test_patch_review_exposes_missing_diff_before_report_submission() -> None:
    tools = ReviewTools({"a.py": "observed()", "CAMPAIGN_PATCH.diff": "diff evidence"}, "")
    verifier = PatchReviewVerifier(tools, PassingChecks(), {"a.py", "CAMPAIGN_PATCH.diff"})
    result = verifier.verify(RunState(RunId("missing-diff")))
    assert not result.passed
    assert "CAMPAIGN_PATCH.diff" in result.summary
    assert "CAMPAIGN_PATCH.diff" in tools.required_intervals


@pytest.mark.parametrize("case", ["sample", "mandatory", "oversized_objective", "context_only"])
def test_report_envelope_compacts_samples_but_never_drops_mandatory_source(case: str) -> None:
    files = {f"source{i}.py": f"observed{i} " + "界" * 700 for i in range(10)}
    tools = ReviewTools(files, "prior report " * 1000)
    for path in files:
        assert call(tools, "read_file", path=path).ok
    if case == "mandatory":
        tools.required_intervals = {path: [(1, 1)] for path in files}
    if case == "context_only":
        tools = ReviewTools({"a.py": "source()"}, "context " * 4000)
        assert call(tools, "read_file", path="a.py").ok
    cfg = CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])
    model = ReportingReviewModel(ScriptedModel([]), tools, cfg, InMemoryEventStore())
    now = datetime(2026, 10, 10, tzinfo=UTC)
    objective = "界" * 10000 if case == "oversized_objective" else "Inspect observed sources"
    context = ModelContext(
        run_id=RunId("packet-fixture"),
        items=(
            ContextItem(
                item_id=ContextItemId("packet-fixture:prior"),
                content="repeated lower-trust context " * 3000,
                trust=TrustClass.UNTRUSTED_CONTENT,
                source=ContextSource(origin=TrustClass.UNTRUSTED_CONTENT, reference="fixture"),
                created_at=now,
            ),
        ),
        assembled_at=now,
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    before = dict(tools.excerpts)
    if case in {"mandatory", "oversized_objective"}:
        with pytest.raises(ModelTurnError) as caught:
            model.report_packet(context, "packet-fixture", objective)
        assert caught.value.reason_code == "REPORT_PACKET_LIMIT"
        assert caught.value.failure_class is ModelFailureClass.PERMANENT
        assert tools.excerpts == before
        return
    packet = model.report_packet(context, "packet-fixture", objective)
    schema = review_module.ShortReferencedReport.model_json_schema()
    assert len(packet.encode()) + len(str(schema).encode()) + len(objective.encode()) <= 26000
    data = json.loads(packet)
    assert data["context_omitted_for_envelope"] is True
    assert data["task_context"] == []
    assert data["earlier_evidence"] == ""
    assert bool(data["omitted_observed_excerpts"]) is (case == "sample")
    assert data["observed"]
    omitted_sources = data["omitted_observed_sources"]
    omitted_ids = [source["excerpt_id"] for source in omitted_sources]
    assert len(omitted_ids) == data["omitted_observed_excerpts"]
    assert all(excerpt_id in tools.references for excerpt_id in omitted_ids)
    assert not set(omitted_ids) & {item["excerpt_id"] for item in data["observed"]}
    assert set(omitted_ids) | {item["excerpt_id"] for item in data["observed"]} == set(
        tools.references
    )
    for item in data["observed"]:
        path, start, text = tools.references[item["excerpt_id"]]
        assert (path, start) == (item["path"], item["start_line"])
        assert item["numbered_text"] == "\n".join(
            f"{number}: {line}" for number, line in enumerate(text.split("\n"), start)
        )
        assert item["end_line"] == start + len(text.split("\n")) - 1
    assert tools.excerpts == before


def test_report_packet_prints_original_nonfirst_page_lines_including_blanks() -> None:
    tools = ReviewTools({"a.py": "unread\n" * 60 + "first\n\nlast", "b.py": "longer\n" * 10}, "")
    assert call(tools, "read_file", path="a.py", start_line="61").ok
    assert call(tools, "read_file", path="b.py").ok
    cfg = CampaignConfig(repository="/fixture", project="brook", revision="a" * 40, scope=["."])
    model = ReportingReviewModel(ScriptedModel([]), tools, cfg, InMemoryEventStore())
    context = ModelContext(
        run_id=RunId("numbered-fixture"),
        items=(),
        assembled_at=datetime(2026, 10, 10, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    packet = model.report_packet(context, "numbered-fixture", "Inspect this source page")
    observed = json.loads(packet)["observed"]
    assert observed[0] == (
        {
            "excerpt_id": "e1",
            "path": "a.py",
            "start_line": 61,
            "end_line": 63,
            "numbered_text": "61: first\n62: \n63: last",
        }
    )
    assert [entry["excerpt_id"] for entry in observed] == ["e1", "e2"]
    assert "unread" not in json.dumps(observed)
    assert tools.references["e1"] == ("a.py", 61, "first\n\nlast")


def test_review_retry_preserves_candidate_and_does_not_consume_edit_attempt() -> None:

    state = reduce_coding(CodingState(), CodingEvent("start"), max_attempts=1)
    state = reduce_coding(state, CodingEvent("patched"), max_attempts=1)
    state = reduce_coding(state, CodingEvent("review"), max_attempts=1)
    state = reduce_coding(state, CodingEvent("failed"), max_attempts=1)
    state = reduce_coding(state, CodingEvent("retry_review"), max_attempts=1)
    assert state.status == "review_pending"
    assert state.attempts == 1
    assert state.review_attempts == 1
    with pytest.raises(ValueError, match="illegal coding transition"):
        reduce_coding(
            CodingState(status="rejected", attempts=3), CodingEvent("retry_review"), max_attempts=3
        )


@pytest.mark.parametrize("required", [True, False])
def test_report_packet_preserves_discovered_lines_in_required_file(required: bool) -> None:
    tools = ReviewTools({"a.py": "required\nunread\ndiscovered", "b.py": "other"}, "")
    tools.register_excerpt("a.py", 1, "required")
    tools.register_excerpt("a.py", 3, "discovered")
    tools.register_excerpt("b.py", 1, "other")
    if required:
        tools.required_intervals = {"a.py": [(1, 1)]}
    context = ModelContext(
        run_id=RunId("discovery-fixture"),
        items=(),
        assembled_at=datetime(2026, 10, 20, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    before = dict(tools.references)
    packet = json.loads(review_module.build_report_packet(tools, context, "Inspect evidence"))
    assert [
        (item["path"], item["start_line"], item["numbered_text"]) for item in packet["observed"]
    ] == [
        ("a.py", 1, "1: required"),
        ("a.py", 3, "3: discovered"),
        ("b.py", 1, "1: other"),
    ]
    assert packet["omitted_observed_excerpts"] == 0
    assert "unread" not in json.dumps(packet["observed"])
    assert tools.references == before


def test_report_compaction_identifies_optional_spans_in_required_observations() -> None:
    files = {f"{index}.py": "required()\n" + "x" * 2900 for index in range(12)}
    tools = ReviewTools(files, "")
    tools.required_intervals = {path: [(1, 1)] for path in files}
    for path in files:
        assert call(tools, "read_file", path=path).ok
    before = dict(tools.references)
    context = ModelContext(
        run_id=RunId("span-compaction"),
        items=(),
        assembled_at=datetime(2026, 10, 20, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    data = json.loads(
        review_module.build_report_packet(tools, context, "Inspect required behavior")
    )
    assert data["omitted_observed_excerpts"] > 0
    retained = {(item["path"], item["start_line"]) for item in data["observed"]}
    assert {(path, 1) for path in files} <= retained
    for omitted in data["omitted_observed_sources"]:
        assert omitted["start_line"] == omitted["end_line"] == 2
        path, start, text = before[omitted["excerpt_id"]]
        assert omitted["path"] == path
        assert start == 1
        assert text == files[path]
        assert (path, 2) not in retained
    assert tools.references == before
    represented = retained | {
        (item["path"], item["start_line"]) for item in data["omitted_observed_sources"]
    }
    assert represented == {(path, number) for path in files for number in (1, 2)}


@pytest.mark.parametrize("blank_count", [1, 2, 3])
def test_report_packet_preserves_required_blank_line_spans(blank_count: int) -> None:
    content = "before\n" + "\n" * blank_count + "after"
    tools = ReviewTools({"a.py": content}, "")
    tools.register_excerpt("a.py", 1, content)
    tools.required_intervals = {"a.py": [(2, blank_count + 1)]}
    context = ModelContext(
        run_id=RunId("blank-span"),
        items=(),
        assembled_at=datetime(2026, 10, 20, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    packet = json.loads(review_module.build_report_packet(tools, context, "Inspect blank lines"))
    required = next(source for source in packet["observed"] if source["start_line"] == 2)
    assert required["end_line"] == blank_count + 1
    assert required["numbered_text"] == "\n".join(f"{i}: " for i in range(2, blank_count + 2))


def test_report_compaction_retains_required_blank_lines_and_discloses_optional_tails() -> None:
    files = {f"{i}.py": "before\n\n" + "x" * 2900 for i in range(12)}
    tools = ReviewTools(files, "")
    tools.required_intervals = {path: [(2, 2)] for path in files}
    for path, content in files.items():
        tools.register_excerpt(path, 1, content)
    context = ModelContext(
        run_id=RunId("blank-compaction"),
        items=(),
        assembled_at=datetime(2026, 10, 20, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    packet = json.loads(review_module.build_report_packet(tools, context, "Inspect blank lines"))
    assert {
        (s["path"], s["start_line"], s["end_line"], s["numbered_text"])
        for s in packet["observed"]
        if s["start_line"] == 2
    } == {(path, 2, 2, "2: ") for path in files}
    assert packet["omitted_observed_sources"]
    assert all(source["start_line"] != 2 for source in packet["omitted_observed_sources"])


@pytest.mark.parametrize("content", ["before\n\n", "\n", "\n\n", ""])
def test_review_verifies_actual_blank_lines_without_inventing_empty_file_lines(
    content: str,
) -> None:
    tools = ReviewTools({"a.py": content}, "")
    tools.required_intervals = {"a.py": [(1, len(content.splitlines()))]}
    assert call(tools, "read_file", path="a.py").ok
    tools.report = ReviewReport.model_validate_json(report())
    assert tools.verify(RunState(RunId("blank-coverage"))).passed
    if not content:
        tools.required_intervals = {"a.py": [(1, 1)]}
        assert not tools.verify(RunState(RunId("invented-line"))).passed


@settings(max_examples=40)
@given(
    st.lists(st.text(alphabet="ab :\t\r\v\f\x85\u2028雪", max_size=20), min_size=1, max_size=10),
    st.integers(min_value=0, max_value=100),
)
def test_report_spans_round_trip_exact_observed_lines(lines: list[str], selector: int) -> None:
    original = ["header", *lines, "tail"]
    content = "\n".join(original)
    tools = ReviewTools({"a.py": content}, "")
    tools.register_excerpt("a.py", 1, content)
    at = selector % len(original) + 1
    tools.required_intervals = {"a.py": [(at, at)]}
    context = ModelContext(
        run_id=RunId("line-property"),
        items=(),
        assembled_at=datetime(2026, 10, 20, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    packet = json.loads(
        review_module.build_report_packet(tools, context, "Preserve exact evidence")
    )
    recovered: dict[int, str] = {}
    for source in packet["observed"]:
        for number, text in enumerate(source["numbered_text"].split("\n"), source["start_line"]):
            prefix, separator, line = text.partition(": ")
            assert separator
            assert int(prefix) == number
            assert number not in recovered
            recovered[number] = line
    assert [recovered[i] for i in range(1, len(original) + 1)] == original
    assert packet["omitted_observed_excerpts"] == 0


@pytest.mark.parametrize("content", ["\n", "nonempty"])
def test_empty_file_coverage_requires_pinned_empty_source(content: str) -> None:
    tools = ReviewTools({"a.py": content}, "")
    tools.required_intervals = {"a.py": [(1, 0)]}
    assert call(tools, "read_file", path="a.py").ok
    tools.report = ReviewReport.model_validate_json(report())
    assert not tools.verify(RunState(RunId("false-empty-file"))).passed


def test_report_compaction_preserves_required_empty_file_metadata() -> None:
    files = {"empty.py": "", **{f"{i}.py": "x" * 2900 for i in range(12)}}
    tools = ReviewTools(files, "")
    tools.required_intervals = {"empty.py": [(1, 0)]}
    for path, content in files.items():
        tools.register_excerpt(path, 1, content)
    context = ModelContext(
        run_id=RunId("empty-compaction"),
        items=(),
        assembled_at=datetime(2026, 10, 20, tzinfo=UTC),
        role=ModelRole.CONTROLLER,
        prompt_template=REVIEW_TEMPLATE.reference(),
    )
    packet = json.loads(review_module.build_report_packet(tools, context, "Inspect empty file"))
    empty = next(source for source in packet["observed"] if source["path"] == "empty.py")
    assert (empty["start_line"], empty["end_line"], empty["numbered_text"]) == (1, 0, "")
    assert packet["omitted_observed_sources"]
    assert all(source["path"] != "empty.py" for source in packet["omitted_observed_sources"])
