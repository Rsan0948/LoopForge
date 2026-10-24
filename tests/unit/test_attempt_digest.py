"""What crosses from a worker attempt to the supervisor: bounded, code-owned, actionable."""

import json
from pathlib import Path

import pytest

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.domain.coding import CodingEvent
from loopforge.domain.state import RunState
from loopforge.domain.types import RunId
from loopforge.entrypoints import campaign_coding as coding
from loopforge.entrypoints.agent_config import GATE_LIMITS
from loopforge.entrypoints.campaign_models import CampaignConfig
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from tests.unit.test_audit_core_regressions import (
    audit_service,  # noqa: F401  # pyright: ignore[reportUnusedImport] - pytest fixture
    no_preflight,
    repository_config,  # noqa: F401  # pyright: ignore[reportUnusedImport] - pytest fixture
    seeded_worker,
)
from tests.unit.worker_trajectory import (
    FEATURES,
    GAME,
    LocalChecks,
    build_task,
    build_workspace,
    marker,
)

CHECKLIST = [
    {
        "probe": "check_acceptance_gameplay",
        "passed": 12,
        "total": 23,
        "next_failing": [{"line": 70, "assertion": "Food must increase score and snake length"}],
    }
]
FAILING = (
    "command:check_acceptance_gameplay: failed (exit_code=1; failing acceptance line 70: "
    "assert ..., 'Food must increase score and snake length')"
)


def test_a_large_failed_attempt_still_tells_the_supervisor_why_and_how_far(
    audit_service: RepositoryGoalService,  # noqa: F811 - pytest fixture
    repository_config: RepositoryGoalConfig,  # noqa: F811 - pytest fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = seeded_worker(audit_service, repository_config)
    monkeypatch.setattr("loopforge.entrypoints.campaign_execution.preflight", no_preflight)
    audit_service.harness.coding.enqueue(worker.campaign, worker.task, repository_config.task)
    detail = {
        "run_id": "run_missing",
        "stop_summary": "CODING_NO_NEW_EVIDENCE: stalled after a warning",
        "last_verification": FAILING + " " + "x" * 9000,
        "patch": "+" * 40_000,
        "patch_sha256": "a" * 64,
        "checklist": CHECKLIST,
        "best_checkpoint_score": 0.61,
    }
    with audit_service.harness.store.connect() as db:
        jobs = audit_service.harness.coding.jobs
        jobs.append(db, worker.task, CodingEvent("start"), "{}")
        jobs.append(db, worker.task, CodingEvent("failed"), json.dumps(detail))

    observation = worker.observe()

    assert len(observation.evidence.encode()) <= GATE_LIMITS.supervisor_evidence_bytes
    evidence = json.loads(observation.evidence)
    assert "patch" not in evidence
    assert evidence["last_verification"].startswith(FAILING)
    assert len(evidence["last_verification"]) <= 6003
    attempt = evidence["attempt"]
    assert attempt["attempts_used"] == 1
    assert attempt["max_attempts"] == repository_config.task.max_attempts
    assert attempt["stop_code"] == "CODING_NO_NEW_EVIDENCE"
    assert attempt["checklist"] == CHECKLIST
    assert attempt["best_checkpoint_score"] == 0.61
    assert evidence["artifact_ids"]


def test_the_checklist_reports_progress_and_the_next_failing_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", LocalChecks)
    repo = build_workspace(tmp_path)
    task = build_task()
    config = CampaignConfig(
        repository=str(repo.root),
        project="snake",
        revision=repo.base_revision,
        scope=["app"],
        rounds=1,
    )
    runtime, _ = coding.build_coding(config, task, repo, store=InMemoryEventStore())
    for name, _ in FEATURES[:4]:
        (repo.root / GAME).write_text((repo.root / GAME).read_text() + marker(name))
    verifier = runtime.verifier
    assert isinstance(verifier, coding.CodingVerifier)
    verifier.verify(RunState(run_id=RunId("r")))
    (entry,) = verifier.checklist()
    assert entry["probe"] == "check_acceptance_gameplay"
    assert (entry["passed"], entry["total"]) == (4, len(FEATURES))
    assert entry["next_failing"][0]["assertion"] == FEATURES[4][1]  # type: ignore[index]
