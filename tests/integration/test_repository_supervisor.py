"""Multi-file goal discovery, missing dependency recovery and restart over real containers."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import loopforge.adapters.gemini_supervisor as gemini_module
from loopforge.adapters.gemini_supervisor import GeminiSupervisor
from loopforge.adapters.scripted import ScriptedModel
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.goal_review import ClarificationPolicy
from loopforge.domain.investigation import FailureKind
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunId
from loopforge.entrypoints.campaign_coding import ConfinedCodingTools
from loopforge.entrypoints.campaign_review import ReviewTools
from loopforge.entrypoints.goal_checkpoint import capture
from loopforge.entrypoints.goal_review import morning_report, review_command
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL
from scripts.qualify_repository_supervisor import fixture_config


@pytest.fixture(autouse=True)
def fixture_pricing_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini_module, "PRICE_VALID_UNTIL", datetime(2100, 1, 1, tzinfo=UTC))


@pytest.fixture
def image() -> str:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "python:3.12-alpine", "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Prepared Docker image unavailable; no pull or host fallback")
    if result.returncode:
        pytest.skip("Prepared Python container image unavailable")
    return result.stdout.strip()


def provider(request: httpx.Request) -> httpx.Response:
    context = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"])
    memory = context["investigation_state"]
    references = {record["reference"]: record["id"] for record in memory["memory"]}
    action: dict[str, object] = {}
    paths = ["src/api.py", "src/quantities.py", "src/report.py"]
    missing = [
        path
        for path in paths
        if not any(reference.startswith(path + ":") for reference in references)
    ]
    if not references:
        action = {"action": "inspect", "inspection": {}}
    elif missing:
        action = {"action": "inspect", "inspection": {"path": missing[0]}}
    elif not memory["environment_ready"]:
        action = {
            "action": "prepare",
            "environment": "stdlib" if memory["research_failures"] else "pytest",
        }
    elif not memory["baseline_reproduced"]:
        action = {"action": "reproduce"}
    elif memory["assignment"] is None:
        action = {
            "action": "delegate",
            "assignment": {
                "objective": "Implement the discovered normalization and reporting behavior",
                "files": ["src/quantities.py", "src/report.py"],
                "evidence_ids": [
                    identity
                    for reference, identity in references.items()
                    if reference.startswith("src/")
                ],
            },
        }
    elif context["worker_evidence_untrusted"]["status"] == "review_pending":
        action = {"action": "verify"}
    else:
        action = {"action": "deliver"}
    decision = {
        **action,
        "rationale": "Original measurements support this authorized action",
        "plan": "Discover, prepare, reproduce, implement both criteria, verify and deliver",
    }
    return httpx.Response(
        200,
        json={
            "modelVersion": "offline-argon-fixture",
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 50,
                "thoughtsTokenCount": 20,
                "totalTokenCount": 170,
            },
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(decision)}]}}
            ],
        },
    )


def driver(runtime: Runtime, run_id: RunId) -> RunState:
    if isinstance(runtime.tools, ConfinedCodingTools):
        runtime.model = ScriptedModel(
            [
                ActionProposal(
                    ActionId("normalize"),
                    "write_file",
                    {
                        "path": "src/quantities.py",
                        "content": (
                            "def normalize_quantity(value):\n    if isinstanc"
                            "e(value, bool) or not isinstance(value, (int, s"
                            "tr)):\n        raise ValueError('invalid quantit"
                            "y')\n    try:\n        return int(value)\n    exce"
                            "pt (ValueError, TypeError):\n        raise Value"
                            "Error('invalid quantity') from None\n"
                        ),
                    },
                ),
                ActionProposal(
                    ActionId("report"),
                    "write_file",
                    {
                        "path": "src/report.py",
                        "content": (
                            "from quantities import normalize_quantity\n\ndef "
                            "summarize(values):\n    normalized = [normalize_"
                            "quantity(value) for value in values]\n    return"
                            " {'count': len(normalized), 'total': sum(normal"
                            "ized)}\n"
                        ),
                    },
                ),
            ]
        )
    else:
        assert isinstance(runtime.tools, ReviewTools)
        runtime.model = ScriptedModel(
            [
                *(
                    ActionProposal(ActionId(f"read-{i}"), "read_file", {"path": path})
                    for i, path in enumerate(sorted(runtime.tools.files))
                ),
                ActionProposal(
                    ActionId("report"),
                    "submit_report",
                    {
                        "report": json.dumps(
                            {
                                "summary": (
                                    "Actual multi-file patch inspected and i"
                                    "ndependent criteria verified"
                                ),
                                "findings": [],
                                "unresolved": [],
                            }
                        )
                    },
                ),
            ]
        )
    return runtime.resume(run_id)


@pytest.mark.parametrize("disposition", [None, "accept", "discard"])
def test_discovered_multi_file_goal_recovers_environment_and_restarts(  # noqa: PLR0915 - complete durable goal story
    tmp_path: Path, image: str, disposition: str | None
) -> None:
    directory = tmp_path / "goal"
    config = fixture_config(tmp_path / "fixture", image)
    original = Path(config.campaign.repository)
    before = None
    if disposition:
        (original / "src/api.py").write_text(
            "# Operator unsaved change\nfrom report import summarize\n"
        )
        (original / "src/user_notes.py").write_text("# User untracked work\n")
        before = capture(original)
        config = config.model_copy(
            update={
                "capture_working_tree": True,
                "clarifications": (
                    ClarificationPolicy(
                        "presentation",
                        "Choose output presentation",
                        "optional",
                        1,
                        "Keep existing API",
                    ),
                ),
            }
        )
    assert config.task.required_changed_paths == []
    assert config.task.allowed_paths == ["src"]
    service = RepositoryGoalService(directory)
    restarted = False
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        supervisor = GeminiSupervisor("fixture-key", client=client)
        try:
            service.create(config)
            if disposition:
                # Simulate absence with a deterministic future clock; no wall-clock wait.
                class AbsentClock:
                    epoch = int(datetime.now(UTC).timestamp())

                    def now(self) -> datetime:
                        return datetime.fromtimestamp(self.epoch, UTC)

                clock = AbsentClock()
                service.clock = clock
                coordinator = service.coordinator(supervisor)
                opened = coordinator.step(GOAL)
                clock.epoch = opened.questions[0].deadline_epoch
                experiment = coordinator.step(GOAL)
                assert len(experiment.experiments) == 1
                assert (
                    experiment.experiments[0].checkpoint == experiment.policy.checkpoint_revision
                    if experiment.policy
                    else False
                )
            service.harness.coding.driver = driver
            coordinator = service.coordinator(supervisor)
            state = service.journal.load(GOAL)
            for _ in range(40):
                state = coordinator.step(GOAL)
                if not restarted and state.baseline_reproduced:
                    expected_ids = [record.record_id for record in state.research]
                    service.close()
                    service = RepositoryGoalService(directory)
                    service.harness.coding.driver = driver
                    coordinator = service.coordinator(supervisor)
                    assert [
                        record.record_id for record in service.journal.load(GOAL).research
                    ] == expected_ids
                    restarted = True
                if state.terminal:
                    break
            assert state.phase == "delivered", state.summary
            assert restarted
            assert any(
                record.kind == "environment" and record.failure == FailureKind.ENVIRONMENT
                for record in state.research
            )
            assert state.environment_id == "stdlib"
            assert state.assignment is not None
            assert set(state.assignment.files) == {"src/quantities.py", "src/report.py"}
            assert all(
                identity in {record.record_id for record in state.research}
                for identity in state.assignment.evidence_ids
            )
            worker = service.worker().prepared()
            assert worker is not None
            with service.harness.store.connect() as db:
                task = service.harness.coding.jobs.state(db, worker.task)
                assert task.status == "patch_ready"
                assert task.attempts == task.review_attempts == 1
            assert (
                Path(config.campaign.repository) / "src/quantities.py"
            ).read_text() == "def normalize_quantity(value):\n    return value\n"
            if disposition:
                assert capture(original) == before
                assert len(state.experiments) == 1
                report = morning_report(directory)
                assert report["diff"]
                assert report["experiments"]
                branch = worker.workspace().run_git("branch", "--show-current").strip()
                assert branch == state.experiments[0].branch
                service.close()
                # A matching late answer adds evidence without invalidating delivery.
                review_command(directory, "answer", "presentation", "Keep existing API")
                result = review_command(directory, disposition)
                assert result["disposition"] == (
                    "accepted" if disposition == "accept" else "discarded"
                )
                assert review_command(directory, disposition) == result
                assert capture(original) == before
                service = RepositoryGoalService(directory)
        finally:
            service.close()
