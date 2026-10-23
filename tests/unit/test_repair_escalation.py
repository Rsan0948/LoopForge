"""Offline specialist thresholds, budgets, strict provider and runtime handoff."""

import hashlib
import json
import math
from pathlib import Path

import httpx
import pytest

from loopforge.adapters.container_sandbox import ContainerSandbox
from loopforge.adapters.gemini_repair import GeminiRepair, RepairDiagnosis, TinyRepair
from loopforge.adapters.git_workspace import GitWorkspace, GitWorkspaceManager
from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.repair_journal import RepairJournal
from loopforge.adapters.scripted import ScriptedModel
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ContextItem, ModelContext
from loopforge.domain.events import (
    ActionProposed,
    ContextAssembled,
    ModelAttemptFailed,
)
from loopforge.domain.repair_escalation import (
    RepairAccount,
    RepairReserved,
    RepairSample,
    RepairSettled,
    escalation_due,
    reduce_repair,
    stall_escalation_due,
)
from loopforge.domain.security import TrustClass
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunStatus, UsageDelta
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints import campaign_coding as coding
from loopforge.entrypoints.campaign_coding import build_coding
from loopforge.entrypoints.campaign_models import CampaignConfig, CodingCheck, CodingTaskConfig
from loopforge.entrypoints.campaign_progress import stagnant_turns
from loopforge.entrypoints.campaign_repair_decisions import (
    DECISION_TOOL,
    RepairDecisionState,
    filter_repair_tools,
)
from loopforge.entrypoints.campaign_repair_escalation import (
    EscalatingRepairModel,
    diagnostic_excerpt,
    failure_identity,
    tiny_patch,
)
from loopforge.ports.model import ModelTurn
from loopforge.ports.sandbox import SandboxCommandResult
from loopforge.ports.supervisor import SupervisorFailure
from loopforge.ports.tools import ToolExecutionRequest
from loopforge.ports.verifier import VerificationResult


@pytest.mark.parametrize(
    ("samples", "due"),
    [
        ([], False),
        ([("a", "1")], False),
        ([("a", "1"), ("a", "1")], True),
        ([("a", "1"), ("b", "1")], False),
        ([("a", "1"), ("a", "2")], False),
        ([("a", "1"), ("a", "2"), ("a", "3")], True),
        ([("a", "1"), ("b", "2"), ("a", "3")], False),
    ],
)
def test_escalation_thresholds(samples: list[tuple[str, str]], due: bool) -> None:
    assert escalation_due(tuple(RepairSample(*s) for s in samples)) is due


def test_failure_identity_ignores_timing_but_retains_actual_business_values() -> None:
    assert failure_identity("assert 100 == 200; failed in 0.13s") == failure_identity(
        "assert 100 == 200; failed in 9.45s"
    )
    assert failure_identity("expected 100 seconds") != failure_identity("expected 200 seconds")
    assert failure_identity("assert 100 == 200") != failure_identity("assert 100 == 201")


def test_repair_reservation_recovery_dedup_and_hard_bounds(tmp_path: Path) -> None:
    path = tmp_path / "repair.sqlite"
    journal = RepairJournal(path, 100)
    journal.append(RepairReserved("failure", 80), "intent")
    restarted = RepairJournal(path, 100)
    with restarted.connect() as db:
        assert restarted.load(db).spent == 80  # Unknown crash outcome stays fully charged.
    with pytest.raises(ValueError, match="reservation denied"):
        restarted.append(RepairReserved("failure", 1), "replay")
    with pytest.raises(ValueError, match="reservation denied"):
        restarted.append(RepairReserved("other", 21), "over budget")
    with pytest.raises(ValueError, match="immutable"):
        RepairJournal(path, 200)
    with pytest.raises(ValueError, match="settlement denied"):
        restarted.append(RepairSettled("failure", 81), "overcharge")
    restarted.append(RepairSettled("failure", 30), "advice")
    with restarted.connect() as db:
        assert restarted.load(db).spent == 30
    assert restarted.latest() == "advice"
    with pytest.raises(ValueError, match="settlement denied"):
        restarted.append(RepairSettled("failure", 0), "double refund")
    account = RepairAccount(1000)
    for key in ("one", "two", "three"):
        account = reduce_repair(account, RepairReserved(key, 1))
    with pytest.raises(ValueError, match="call cap"):
        reduce_repair(account, RepairReserved("four", 1))


@pytest.mark.parametrize(
    ("old", "new", "allowed"),
    [
        ("x=1\n", "x=2\n", True),
        ("a\nb\n", "c\nd\n", True),
        ("a\nb\nc\n", "d\ne\nf\n", False),
        ("same", "same", False),
    ],
)
def test_tiny_patch_size_is_measured(old: str, new: str, allowed: bool) -> None:
    diagnosis = RepairDiagnosis(
        diagnosis="d",
        evidence="e",
        next_steps="n",
        verification="v",
        patch=TinyRepair(path="app/a.py", sha256="a" * 64, old=old, new=new),
    )
    assert tiny_patch(diagnosis) is allowed


def envelope(diagnosis: dict[str, object]) -> dict[str, object]:
    return {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps(diagnosis)}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 20,
            "totalTokenCount": 120,
        },
    }


@pytest.mark.parametrize(
    "mode",
    [
        "patch",
        "advice",
        "large",
        "scope",
        "stale",
        "invalid",
        "http",
        "usage",
        "secret",
        "budget",
        "tokens",
        "deadline",
    ],
)
def test_specialist_runtime_handoff_and_denials(  # noqa: PLR0915 - full runtime boundary scenario
    tmp_path: Path, mode: str
) -> None:
    workspace = GitWorkspaceManager(tmp_path / "source").materialize(
        FixtureSpec(
            fixture_id="specialist",
            files=(FixtureFile(path="app/small.py", content="value = 0\n"),),
        )
    )
    task = CodingTaskConfig(
        objective="Repair the incorrect value with independent checks",
        kind="bugfix",
        branch="goal/escalation",
        image="sha256:" + "a" * 64,
        allowed_paths=["app"],
        required_changed_paths=["app/small.py"],
        evidence="fixture",
        checks=[CodingCheck(name="check_project", argv=["/usr/local/bin/python", "-c", "pass"])],
    )
    config = CampaignConfig(
        repository=str(workspace.root),
        project="reconciliation",
        revision=workspace.base_revision,
        scope=["app"],
        inference_profile="qwen-state-v1",
        repair_escalation=True,
        max_iterations=12,
    )
    if mode == "budget":
        config = config.model_copy(update={"repair_budget_microusd": 1})
    if mode == "tokens":
        config = config.model_copy(update={"max_tokens": 1000})
    if mode == "deadline":
        config = config.model_copy(update={"max_seconds": 180})
    calls: list[bytes] = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request.content)
        packet = json.loads(json.loads(calls[-1])["contents"][0]["parts"][0]["text"])
        assert "orchestrator" not in packet
        assert "test-key" not in request.content.decode()
        source = next(s for s in packet["source_untrusted"] if s["path"] == "app/small.py")
        assert source["source"] == "value = 3\n"  # Escalate only after actual failed repairs.
        diagnosis: dict[str, object] = {
            "diagnosis": "The value must be four",
            "evidence": "Three fixed checks failed",
            "next_steps": "Set the value to four",
            "verification": "Run check_project",
            "patch": None,
        }
        if mode in {"patch", "large", "scope", "stale"}:
            diagnosis["patch"] = {
                "path": "outside.py" if mode == "scope" else "app/small.py",
                "sha256": "0" * 64 if mode == "stale" else source["sha256"],
                "old": "value = 3\n",
                "new": "value = 4\n" if mode != "large" else "value = 4\nx=1\ny=2\n",
            }
        if mode == "invalid":
            diagnosis["authority"] = "runtime_policy"
        if mode == "secret":
            diagnosis["diagnosis"] = "test-key"
        payload = envelope(diagnosis)
        if mode == "usage":
            payload["usageMetadata"] = {
                "promptTokenCount": 1000000,
                "candidatesTokenCount": 20,
                "totalTokenCount": 1000020,
            }
        return httpx.Response(503 if mode == "http" else 200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(response))
    specialist = GeminiRepair("test-key", client)
    proposals = [
        ActionProposal(
            ActionId(f"local-{value}"),
            "apply_edits",
            {
                "path": "app/small.py",
                "sha256": hashlib.sha256(f"value = {value - 1}\n".encode()).hexdigest(),
                "edits": json.dumps(
                    [{"old": f"value = {value - 1}\n", "new": f"value = {value}\n"}]
                ),
            },
        )
        for value in range(1, 5)
    ]
    store = InMemoryEventStore()
    runtime, _ = build_coding(
        config,
        task,
        workspace,
        store=store,
        model=ScriptedModel(proposals),
        repair_ledger=tmp_path / "repair/ledger.sqlite",
    )
    assert isinstance(runtime.model, EscalatingRepairModel)
    runtime.model.specialist = specialist

    class ValueVerifier:
        calls = 0

        def verify(self, state: RunState) -> VerificationResult:
            del state
            self.calls += 1
            passed = (workspace.root / "app/small.py").read_text() == "value = 4\n"
            return VerificationResult(
                passed, "fixed value" if passed else f"same incorrect value in {self.calls}.01s"
            )

    runtime.verifier = ValueVerifier()
    try:
        state = runtime.run(task.objective)
    finally:
        client.close()
    assert state.status == RunStatus.SUCCEEDED
    assert len(calls) == (0 if mode in {"budget", "tokens", "deadline"} else 1)
    events = store.events_for(state.run_id)
    actions = [e.proposal for e in events if isinstance(e, ActionProposed)]
    assert [p.action_id for p in actions[:3]] == [ActionId(f"local-{i}") for i in range(1, 4)]
    if mode in {"budget", "tokens", "deadline"}:
        assert actions[3].action_id == ActionId("local-4")
        return
    assert actions[3].tool_name == ("apply_edits" if mode == "patch" else "record_repair_guidance")
    assert state.cost_usd > 0
    ledger = RepairJournal(tmp_path / "repair/ledger.sqlite", 500000)
    with ledger.connect() as db:
        reserved = db.execute("SELECT COUNT(*) FROM events WHERE kind='reserved'").fetchone()[0]
        assert reserved == 1
        account = ledger.load(db)
    assert math.isclose(
        state.cost_usd, (3 if mode == "patch" else 4) * 0.01 + account.spent / 1_000_000
    )
    assert state.input_tokens > (3 if mode == "patch" else 4) * 100
    contexts = [e for e in events if isinstance(e, ContextAssembled)]
    if mode != "patch":
        guidance = next(
            i
            for e in contexts
            for i in e.context_items
            if str(i.item_id).endswith(":repair-specialist")
        )
        assert guidance.trust == TrustClass.MODEL_INFERENCE
        assert guidance.source.origin == TrustClass.MODEL_INFERENCE


def test_repair_config_preserves_disabled_canonical_binding() -> None:
    config = CampaignConfig(
        repository="/tmp/source", project="brook", revision="a" * 40, scope=["src"]
    )
    assert "repair_escalation" not in config.model_dump()
    assert CampaignConfig.model_validate_json(config.model_dump_json()) == config
    with pytest.raises(ValueError, match="fresh-state"):
        CampaignConfig.model_validate(config.model_dump() | {"repair_escalation": True})


def test_provider_admission_rejects_oversize_and_credentials() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        model = GeminiRepair("private-key", client)
        for packet in ("x" * 48000, "private-key"):
            with pytest.raises(SupervisorFailure, match="input denied"):
                model.prepare(packet)


class FailedChecks(ContainerSandbox):
    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        del command_name, timeout_seconds
        return SandboxCommandResult(
            exit_code=1,
            succeeded=False,
            stderr="",
            stdout="frame\n" * 2000
            + 'File "/workspace/app/small.py", line 1\n'
            + "ValueError: expected four, got zero\n"
            + "frame\n" * 2000,
        )


def progress_fixture(tmp_path: Path) -> tuple[GitWorkspace, CodingTaskConfig, CampaignConfig]:
    workspace = GitWorkspaceManager(tmp_path / "source").materialize(
        FixtureSpec(
            fixture_id="stagnation",
            files=(FixtureFile(path="app/small.py", content="value = 0\n"),),
        )
    )
    task = CodingTaskConfig(
        objective="Repair the value after a recorded read loop",
        kind="bugfix",
        branch="goal/stagnation",
        image="sha256:" + "a" * 64,
        allowed_paths=["app"],
        required_changed_paths=["app/small.py"],
        evidence="fixed failed check",
        checks=[CodingCheck(name="check_project", argv=["/usr/local/bin/python", "-c", "pass"])],
    )
    config = CampaignConfig(
        repository=str(workspace.root),
        project="reconciliation",
        revision=workspace.base_revision,
        scope=["app"],
        inference_profile="qwen-state-v1",
        repair_escalation=True,
        max_iterations=32,
    )
    return workspace, task, config


class ValueVerifier:
    def __init__(self, root: Path) -> None:
        self.root = root

    def verify(self, state: RunState) -> VerificationResult:
        del state
        passed = (self.root / "app/small.py").read_text() == "value = 4\n"
        return VerificationResult(passed, "fixed" if passed else "incorrect value")


class ReadUntilAdvice(ScriptedModel):
    def __init__(self, root: Path) -> None:
        super().__init__([])
        self.root, self.calls, self.after_advice = root, 0, 0

    def propose_action(self, context: ModelContext) -> ModelTurn:
        self.calls += 1
        if any(str(i.item_id).endswith(":repair-specialist") for i in context.items):
            self.after_advice += 1
        if self.after_advice > 3:
            raw = (self.root / "app/small.py").read_bytes()
            proposal = ActionProposal(
                ActionId(f"fix-{self.calls}"),
                "apply_edits",
                {
                    "path": "app/small.py",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "edits": json.dumps([{"old": raw.decode(), "new": "value = 4\n"}]),
                },
            )
        else:
            proposal = ActionProposal(
                ActionId(f"read-{self.calls}"), "read_file", {"path": "app/small.py"}
            )
        return ModelTurn(proposal, UsageDelta())


@pytest.mark.parametrize("patch", [True, False])
def test_read_stall_escalates_before_stop_and_advice_allows_useful_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: bool
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    task = task.model_copy(
        update={
            "checks": [
                CodingCheck(name="check_documents", argv=["/usr/local/bin/python", "-c", "pass"]),
                *task.checks,
            ]
        }
    )
    store, local = InMemoryEventStore(), ReadUntilAdvice(workspace.root)
    runtime, _ = build_coding(
        config,
        task,
        workspace,
        store=store,
        model=local,
        repair_ledger=tmp_path / "repair/ledger.sqlite",
    )
    runtime.verifier = ValueVerifier(workspace.root)
    calls: list[bytes] = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request.content)
        packet = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"])
        assert 6 <= packet["escalation_evidence"]["stagnant_turns"] < 8
        assert "ValueError: expected four" in packet["reproduced_check_output_untrusted"]
        source = packet["source_untrusted"][0]
        return httpx.Response(
            200,
            json=envelope(
                {
                    "diagnosis": "Set value to four",
                    "evidence": "Reproduced fixed check failed",
                    "next_steps": "Read exact source if needed, then set the value to four",
                    "verification": "Run fixed checks",
                    "patch": {
                        "path": source["path"],
                        "sha256": source["sha256"],
                        "old": "value = 0\n",
                        "new": "value = 4\n",
                    }
                    if patch
                    else None,
                }
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert isinstance(runtime.model, EscalatingRepairModel)
        runtime.model.specialist = GeminiRepair("test-key", client)
        state = runtime.run(task.objective)
    assert state.status is RunStatus.SUCCEEDED
    assert len(calls) == 1
    events = store.events_for(state.run_id)
    assert not any(isinstance(e, ModelAttemptFailed) for e in events)
    assert any(
        isinstance(e, ActionProposed) and str(e.proposal.action_id).startswith("repair-reproduce-")
        for e in events
    )
    reproduction = next(
        e.proposal
        for e in events
        if isinstance(e, ActionProposed)
        and str(e.proposal.action_id).startswith("repair-reproduce-")
    )
    assert reproduction.tool_name == "check_project"
    assert len(list((tmp_path / "repair").glob("check-output-*.txt"))) == 1
    decision_state = RepairDecisionState(
        store,
        RepairJournal(tmp_path / "repair/ledger.sqlite", 500000),
        lambda: coding.patch_identity(workspace),
    )
    receipt = decision_state.applied_specialist_action()
    assert (receipt is not None) is patch
    if receipt is not None:
        assert receipt["path"] == "app/small.py"
    if not patch:
        assert local.after_advice == 4  # New guidance buys a real bounded investigation window.
        assert stagnant_turns(events) < 8


@pytest.mark.parametrize("mode", ["recover", "changed", "missing", "not_stalled"])
def test_frozen_stall_handoff_requires_terminal_proof_and_matching_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    store = InMemoryEventStore()
    old_config = config.model_copy(
        update={"repair_escalation": False, "max_iterations": 2 if mode == "not_stalled" else 32}
    )
    old, _ = build_coding(
        old_config, task, workspace, store=store, model=ReadUntilAdvice(workspace.root)
    )
    old.verifier = ValueVerifier(workspace.root)
    stopped = old.run(task.objective)
    assert stopped.status is RunStatus.FAILED
    old_events = store.events_for(stopped.run_id)
    if mode == "changed":
        (workspace.root / "app/small.py").write_text("value = 1\n")
    config = config.model_copy(
        update={
            "repair_recovery_run_id": "run_missing" if mode == "missing" else str(stopped.run_id)
        }
    )
    raw = (workspace.root / "app/small.py").read_bytes()
    local = ScriptedModel(
        [
            ActionProposal(
                ActionId("local-fix"),
                "apply_edits",
                {
                    "path": "app/small.py",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "edits": json.dumps([{"old": raw.decode(), "new": "value = 4\n"}]),
                },
            )
        ]
    )
    runtime, _ = build_coding(
        config,
        task,
        workspace,
        store=store,
        model=local,
        repair_ledger=tmp_path / "repair/ledger.sqlite",
    )
    runtime.verifier = ValueVerifier(workspace.root)
    requests: list[bytes] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        packet = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"])
        assert packet["escalation_evidence"]["recorded_stall_run_id"] == str(stopped.run_id)
        source = packet["source_untrusted"][0]
        return httpx.Response(
            200,
            json=envelope(
                {
                    "diagnosis": "Fix value",
                    "evidence": "Recorded stall and fresh check",
                    "next_steps": "Use four",
                    "verification": "Run fixed checks",
                    "patch": {
                        "path": source["path"],
                        "sha256": source["sha256"],
                        "old": "value = 0\n",
                        "new": "value = 4\n",
                    },
                }
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert isinstance(runtime.model, EscalatingRepairModel)
        runtime.model.specialist = GeminiRepair("test-key", client)
        state = runtime.run(task.objective)
    assert state.status is RunStatus.SUCCEEDED
    assert len(requests) == (1 if mode == "recover" else 0)
    assert store.events_for(stopped.run_id) == old_events  # No terminal-state reactivation.
    if mode == "recover":
        first = next(
            e.proposal for e in store.events_for(state.run_id) if isinstance(e, ActionProposed)
        )
        assert str(first.action_id).startswith("repair-reproduce-")


def test_stall_threshold_and_middle_error_evidence() -> None:
    assert not stall_escalation_due(5)
    assert stall_escalation_due(6)
    original = "frame\n" * 2000 + "RuntimeError: useful middle evidence\n" + "frame\n" * 2000
    excerpt = diagnostic_excerpt(original)
    assert "RuntimeError: useful middle evidence" in excerpt
    assert len(excerpt) < 8000


def test_repeated_guidance_acknowledgements_cannot_reset_the_stop_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    config = config.model_copy(update={"repair_budget_microusd": 1})
    store = InMemoryEventStore()
    local = ScriptedModel(
        [
            ActionProposal(ActionId(f"fake-guidance-{i}"), "record_repair_guidance", {})
            for i in range(24)
        ]
    )
    runtime, _ = build_coding(
        config,
        task,
        workspace,
        store=store,
        model=local,
        repair_ledger=tmp_path / "repair/ledger.sqlite",
    )
    runtime.verifier = ValueVerifier(workspace.root)
    requests: list[bytes] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert isinstance(runtime.model, EscalatingRepairModel)
        runtime.model.specialist = GeminiRepair("test-key", client)
        state = runtime.run(task.objective)
    assert state.status is RunStatus.FAILED
    assert not requests  # Unfunded escalation cannot consume the provider budget.
    assert any(
        isinstance(e, ModelAttemptFailed) and e.reason_code == "CODING_NO_NEW_EVIDENCE"
        for e in store.events_for(state.run_id)
    )


def decision_action(key: str = "decision", *, status: str = "investigate") -> ActionProposal:
    return ActionProposal(
        ActionId(key),
        DECISION_TOOL,
        {
            "target_defect": "Incorrect value",
            "path": "app/small.py",
            "missing_evidence": "Read the exact value",
            "next_action": "Set four, then run fixed checks",
            "status": status,
        },
    )


def value_fix(root: Path) -> ActionProposal:
    raw = (root / "app/small.py").read_bytes()
    return ActionProposal(
        ActionId("local-fix"),
        "apply_edits",
        {
            "path": "app/small.py",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "edits": json.dumps([{"old": raw.decode(), "new": "value = 4\n"}]),
        },
    )


@pytest.mark.parametrize("mode", ["valid", "fourth_read", "reword", "unable", "missing"])
def test_repair_decision_is_durable_and_investigation_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    config = config.model_copy(update={"repair_decisions": True})
    store = InMemoryEventStore()
    reads = [
        ActionProposal(ActionId(f"read-{i}"), "read_file", {"path": "app/small.py"})
        for i in range(12)
    ]
    actions = [decision_action(status="unable" if mode == "unable" else "investigate"), *reads[:3]]
    if mode == "fourth_read":
        actions.append(reads[3])
    if mode == "reword":
        actions.append(decision_action("reworded"))
    actions.append(value_fix(workspace.root))
    if mode == "missing":
        actions = reads
    runtime, _ = build_coding(
        config,
        task,
        workspace,
        store=store,
        model=ScriptedModel(actions),
        repair_ledger=tmp_path / "repair/ledger.sqlite",
    )
    runtime.verifier = ValueVerifier(workspace.root)
    result = runtime.run(task.objective)
    events = store.events_for(result.run_id)
    assert result.status is (
        RunStatus.FAILED if mode in {"unable", "missing"} else RunStatus.SUCCEEDED
    )
    proposals = [e.proposal for e in events if isinstance(e, ActionProposed)]
    assert sum(p.tool_name == "read_file" for p in proposals) <= 3
    assert sum(p.tool_name == DECISION_TOOL for p in proposals) == (0 if mode == "missing" else 1)
    if mode in {"fourth_read", "reword", "missing"}:
        assert any(
            isinstance(e, ModelAttemptFailed) and e.reason_code == "REPAIR_DECISION_REQUIRED"
            for e in events
        )
    contexts = [e for e in events if isinstance(e, ContextAssembled)]
    recorded = [
        i for e in contexts for i in e.context_items if str(i.item_id).endswith(":repair-decision")
    ]
    if mode != "missing":
        assert recorded
        assert all(
            i.trust is TrustClass.MODEL_INFERENCE and i.source.origin is TrustClass.MODEL_INFERENCE
            for i in recorded
        )
    if mode == "valid":
        last_policy = next(
            i
            for i in contexts[-1].context_items
            if str(i.item_id).endswith(":repair-decision-policy")
        )
        assert json.loads(last_policy.content)["investigation_actions_used"] == 3
    if mode == "unable":
        assert any(
            isinstance(e, ModelAttemptFailed) and e.reason_code == "CODING_REPAIR_UNABLE"
            for e in events
        )
        retry, _ = build_coding(
            config,
            task,
            workspace,
            store=store,
            model=ScriptedModel([]),
            prior_runs=(result.run_id,),
            repair_ledger=tmp_path / "repair/ledger.sqlite",
        )
        retry.verifier = ValueVerifier(workspace.root)
        denied = retry.run(task.objective)
        assert denied.status is RunStatus.FAILED
        assert not any(isinstance(e, ActionProposed) for e in store.events_for(denied.run_id))


def test_repair_decision_survives_runtime_and_store_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    config = config.model_copy(update={"repair_decisions": True})
    store = SQLiteEventStore(tmp_path / "runs.sqlite", codec=JsonEventCodec())
    ledger_path = tmp_path / "repair/ledger.sqlite"
    first, _ = build_coding(
        config,
        task,
        workspace,
        store=store,
        model=ScriptedModel([decision_action()]),
        repair_ledger=ledger_path,
    )
    first.verifier = ValueVerifier(workspace.root)
    run_id = first.start(task.objective)
    assert not first.step(run_id).status.is_terminal
    reopened = SQLiteEventStore(tmp_path / "runs.sqlite", codec=JsonEventCodec())
    second, _ = build_coding(
        config,
        task,
        workspace,
        store=reopened,
        model=ScriptedModel([value_fix(workspace.root)]),
        repair_ledger=ledger_path,
    )
    second.verifier = ValueVerifier(workspace.root)
    assert second.resume(run_id).status is RunStatus.SUCCEEDED
    assert (
        sum(
            isinstance(e, ActionProposed) and e.proposal.tool_name == DECISION_TOOL
            for e in reopened.events_for(run_id)
        )
        == 1
    )


def test_decision_tool_rejects_unjournaled_or_out_of_scope_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    config = config.model_copy(update={"repair_decisions": True})
    runtime, _ = build_coding(
        config,
        task,
        workspace,
        store=InMemoryEventStore(),
        model=ScriptedModel([]),
        repair_ledger=tmp_path / "repair/ledger.sqlite",
    )
    assert not runtime.tools.execute(ToolExecutionRequest(decision_action(), 1, 5)).ok
    args = dict(decision_action().arguments) | {"path": "../outside.py"}
    assert not runtime.tools.execute(
        ToolExecutionRequest(ActionProposal(ActionId("bad"), DECISION_TOOL, args), 1, 5)
    ).ok
    args = dict(decision_action().arguments) | {"permission": "critical"}
    assert not runtime.tools.execute(
        ToolExecutionRequest(ActionProposal(ActionId("bad"), DECISION_TOOL, args), 1, 5)
    ).ok


@pytest.mark.parametrize("denied", ["read", "check", "missing_guess", "reword"])
def test_forced_best_guess_requires_edit_and_preserves_runtime_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: str
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    config = config.model_copy(update={"repair_decisions": True, "force_repair_attempt": True})
    decision = decision_action()
    args = dict(decision.arguments) | {"best_guess": "Replace value = 0 with value = 4"}
    valid = ActionProposal(ActionId("guess"), DECISION_TOOL, args)
    reads = [
        ActionProposal(ActionId(f"read-{i}"), "read_file", {"path": "app/small.py"})
        for i in range(2)
    ]
    bad = (
        decision
        if denied == "missing_guess"
        else ActionProposal(ActionId("again"), DECISION_TOOL, args)
        if denied == "reword"
        else ActionProposal(ActionId("extra"), "read_file", {"path": "app/small.py"})
        if denied == "read"
        else ActionProposal(ActionId("check"), "check_project", {})
    )
    actions = [bad, valid, *reads] if denied == "missing_guess" else [valid, *reads, bad]
    store = InMemoryEventStore()
    runtime, _ = build_coding(
        config,
        task,
        workspace,
        store=store,
        model=ScriptedModel([*actions, value_fix(workspace.root)]),
        repair_ledger=tmp_path / "repair/ledger.sqlite",
    )
    runtime.verifier = ValueVerifier(workspace.root)
    result = runtime.run(task.objective)
    events = store.events_for(result.run_id)
    assert result.status is RunStatus.SUCCEEDED
    failures = [e for e in events if isinstance(e, ModelAttemptFailed)]
    assert len(failures) == 1
    assert "Rejected tool" in failures[0].summary
    contexts = [e for e in events if isinstance(e, ContextAssembled)]
    act_context = next(
        e
        for e in contexts
        if any(
            i.source.reference == "runtime:repair-decision-policy"
            and json.loads(i.content)["phase"] == "act"
            for i in e.context_items
        )
    )
    context = ModelContext(
        run_id=result.run_id,
        items=tuple(
            ContextItem(
                item_id=i.item_id,
                content=i.content,
                trust=i.trust,
                source=i.source,
                created_at=i.created_at,
            )
            for i in act_context.context_items
        ),
        assembled_at=act_context.occurred_at,
    )
    assert filter_repair_tools(
        context, frozenset({DECISION_TOOL, "apply_edits", "read_file", "check_project"})
    ) == frozenset({DECISION_TOOL, "apply_edits"})
    assert (
        sum(isinstance(e, ActionProposed) and e.proposal.tool_name == "read_file" for e in events)
        == 2
    )
    assert not any(
        isinstance(e, ActionProposed) and e.proposal.action_id == bad.action_id for e in events
    )


def test_forced_attempt_cannot_enable_itself_without_decision_authority(tmp_path: Path) -> None:
    _, _, config = progress_fixture(tmp_path)
    with pytest.raises(ValueError, match="Forced repair attempts require"):
        CampaignConfig.model_validate(config.model_dump() | {"force_repair_attempt": True})
    assert "force_repair_attempt" not in config.model_dump()


def test_real_request_recovers_from_guard_rejection_and_repairs_two_cycles(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike a scripted sequence, the response depends on serialized feedback."""
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    config = config.model_copy(update={"repair_decisions": True, "force_repair_attempt": True})
    store = InMemoryEventStore()
    count = 0
    guess = dict(decision_action().arguments) | {"best_guess": "Set value to four"}

    def response(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        text = "\n".join(m["content"] for m in payload["messages"])
        specs = {t["function"]["name"]: t["function"] for t in payload["tools"]}
        statuses = specs[DECISION_TOOL]["parameters"]["properties"]["status"]["enum"]
        assert payload["think"] is False
        if count == 1:
            assert set(specs) == {DECISION_TOOL}
            name, args = "read_file", {"path": "app/small.py"}
        elif count == 2:
            assert "previous_model_attempt_failed" in text
            assert "Rejected tool read_file: Missing decision" in text
            assert '"action_executed": false' in text
            name, args = DECISION_TOOL, guess
        elif count == 3:
            assert "previous_model_attempt_failed" not in text
            assert statuses == ["unable"]
            assert "The repair decision is already recorded" in text
            # Deliberately violate the advertised schema to exercise the guard.
            name, args = DECISION_TOOL, guess
        elif count == 4:
            assert "Rejected tool record_repair_decision: Decision already recorded" in text
            name, args = "read_file", {"path": "app/small.py"}
        elif count == 5:
            assert "previous_model_attempt_failed" not in text
            name, args = "read_file", {"path": "app/small.py"}
        elif count in {6, 9}:
            if count == 6:
                assert '"phase": "act"' in text
                assert statuses == ["unable"]
                assert "Record best_guess:" not in text
                assert "read_file" not in specs
            raw = (workspace.root / "app/small.py").read_text()
            name, args = (
                "apply_edits",
                {
                    "path": "app/small.py",
                    "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "edits": json.dumps(
                        [{"old": raw, "new": f"value = {2 if count == 6 else 4}\n"}]
                    ),
                },
            )
        elif count == 7:
            assert set(specs) == {DECISION_TOOL}
            assert '"recent_successful_edits"' in text
            assert "exact_arguments" in text
            assert "incorrect value" in text
            name, args = DECISION_TOOL, guess
        elif count == 8:
            name, args = "read_file", {"path": "app/small.py"}
        else:
            pytest.fail(f"Unexpected extra inference {count}")
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": name, "arguments": args}}],
                },
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 10,
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", response)
    runtime, live = build_coding(
        config, task, workspace, store=store, repair_ledger=tmp_path / "repair/ledger.sqlite"
    )
    runtime.verifier = ValueVerifier(workspace.root)
    assert live is not None
    try:
        state = runtime.run(task.objective)
    finally:
        live.close()
    assert state.status is RunStatus.SUCCEEDED
    assert count == 9
    assert (
        len([e for e in store.events_for(state.run_id) if isinstance(e, ModelAttemptFailed)]) == 2
    )


def test_revised_decision_preserves_consumed_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    config = config.model_copy(
        update={
            "repair_decisions": True,
            "force_repair_attempt": True,
            "revisable_repair_decisions": True,
        }
    )
    initial = decision_action()
    initial = ActionProposal(
        initial.action_id,
        initial.tool_name,
        dict(initial.arguments) | {"best_guess": "Wrong initial guess"},
    )
    revised = ActionProposal(
        ActionId("revision"),
        DECISION_TOOL,
        dict(initial.arguments) | {"best_guess": "Set value to four"},
    )
    reads = [
        ActionProposal(ActionId(f"read-{n}"), "read_file", {"path": "app/small.py"})
        for n in range(3)
    ]
    store = InMemoryEventStore()
    runtime, _ = build_coding(
        config,
        task,
        workspace,
        store=store,
        model=ScriptedModel(
            [initial, reads[0], revised, reads[1], reads[2], value_fix(workspace.root)]
        ),
        repair_ledger=tmp_path / "repair/ledger.sqlite",
    )
    runtime.verifier = ValueVerifier(workspace.root)
    state = runtime.run(task.objective)
    assert state.status is RunStatus.SUCCEEDED
    events = store.events_for(state.run_id)
    assert any(
        isinstance(e, ActionProposed) and e.proposal.action_id == revised.action_id for e in events
    )
    failures = [e for e in events if isinstance(e, ModelAttemptFailed)]
    assert len(failures) == 1
    assert "Best-guess attempt required" in failures[0].summary
    contexts = [e for e in events if isinstance(e, ContextAssembled)]
    assert any(
        '"investigation_actions_used": 2' in i.content for e in contexts for i in e.context_items
    )


def test_revisable_decisions_require_frozen_authority(tmp_path: Path) -> None:
    _, _, config = progress_fixture(tmp_path)
    assert "revisable_repair_decisions" not in config.model_dump()
    with pytest.raises(ValueError, match="Revisable decisions require"):
        CampaignConfig.model_validate(config.model_dump() | {"revisable_repair_decisions": True})


def test_identical_revisable_decision_returns_feedback_then_allows_real_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    config = config.model_copy(
        update={"repair_decisions": True, "revisable_repair_decisions": True}
    )
    store = InMemoryEventStore()
    calls = 0
    initial = dict(decision_action().arguments)

    def response(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        text = "\n".join(m["content"] for m in payload["messages"])
        specs = {t["function"]["name"] for t in payload["tools"]}
        if calls in {1, 3}:
            name, args = DECISION_TOOL, initial
        elif calls == 2:
            name, args = "read_file", {"path": "app/small.py"}
        elif calls == 4:
            assert "Identical decision is not progress" in text
            assert '"action_executed": false' in text
            assert "check_project" in specs
            name, args = DECISION_TOOL, initial | {"best_guess": "Observed zero; replace with four"}
        elif calls == 5:
            assert "previous_model_attempt_failed" not in text
            proposal = value_fix(workspace.root)
            name, args = proposal.tool_name, dict(proposal.arguments)
        else:
            pytest.fail("Duplicate decision should recover without extra inference")
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": name, "arguments": args}}],
                },
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 10,
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", response)
    runtime, live = build_coding(
        config, task, workspace, store=store, repair_ledger=tmp_path / "repair/ledger.sqlite"
    )
    runtime.verifier = ValueVerifier(workspace.root)
    assert live is not None
    try:
        state = runtime.run(task.objective)
    finally:
        live.close()
    assert state.status is RunStatus.SUCCEEDED
    assert calls == 5
    decisions = [
        e
        for e in store.events_for(state.run_id)
        if isinstance(e, ActionProposed) and e.proposal.tool_name == DECISION_TOOL
    ]
    assert len(decisions) == 2


@pytest.mark.parametrize("current", [True, False])
def test_superseded_specialist_instructions_are_omitted_but_current_advice_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: bool
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FailedChecks)
    workspace, task, config = progress_fixture(tmp_path)
    ledger = tmp_path / "repair/ledger.sqlite"
    journal = RepairJournal(ledger, config.repair_budget_microusd)
    journal.append(RepairReserved("historical", 1), "intent")
    journal.append(
        RepairSettled("historical", 1),
        json.dumps(
            {
                "basis_patch_sha256": coding.patch_identity(workspace) if current else "a" * 64,
                "diagnosis": {"diagnosis": "Old diagnosis", "next_steps": "OBSOLETE_EDIT_SENTINEL"},
            }
        ),
    )
    runtime, _ = build_coding(
        config,
        task,
        workspace,
        store=InMemoryEventStore(),
        model=ScriptedModel([]),
        repair_ledger=ledger,
    )
    run_id = runtime.start(task.objective)
    context = runtime.context.build_context(runtime.state_for(run_id))
    advice = next(i for i in context.items if str(i.item_id).endswith(":repair-specialist"))
    assert ("OBSOLETE_EDIT_SENTINEL" in advice.content) is current
    assert advice.trust is TrustClass.MODEL_INFERENCE
    if not current:
        assert "historical_advice_sha256" in advice.content
        assert "OBSOLETE_EDIT_SENTINEL" in (journal.latest() or "")
