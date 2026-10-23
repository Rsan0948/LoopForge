"""Offline regressions for evidence admission, paging and milestone recovery."""

import hashlib
import inspect
import json
import stat
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast

import pytest
import test_repository_investigation as investigation_tests

from loopforge.adapters.gemini_supervisor import MAX_REQUEST_BYTES, GeminiSupervisor
from loopforge.application.supervisor import GoalCoordinator
from loopforge.domain.goals import GoalAction, GoalState, SupervisorDecision
from loopforge.domain.investigation import InspectionRequest
from loopforge.entrypoints.campaign_coding import acceptance_contract_hash, local_git
from loopforge.entrypoints.campaign_models import CodingTaskConfig
from loopforge.entrypoints.goal_checkpoint import capture, snapshot_checkpoint
from loopforge.entrypoints.goal_worker import CampaignGoalWorker
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL
from loopforge.ports.supervisor import SupervisorRequest, SupervisorResponse


def no_preflight(_task: CodingTaskConfig) -> None:
    pass


@pytest.fixture
def repository_config(tmp_path: Path) -> RepositoryGoalConfig:
    return cast(
        "Callable[[Path], RepositoryGoalConfig]", inspect.unwrap(investigation_tests.config)
    )(tmp_path)


@pytest.fixture
def audit_service(
    tmp_path: Path, repository_config: RepositoryGoalConfig
) -> Generator[RepositoryGoalService, None, None]:
    service = RepositoryGoalService(tmp_path / "audit-goal")
    try:
        service.create(repository_config)
        yield service
    finally:
        service.close()


class DispatchReached(Exception):
    """Stop immediately before any worker or provider execution."""


def seeded_worker(
    service: RepositoryGoalService, config: RepositoryGoalConfig
) -> CampaignGoalWorker:
    source = Path(config.campaign.repository)
    (source / "src/parse.py").write_text("def parse(value): return int(value)\n")
    (source / "src/parse.py").chmod(0o755)
    (source / "src/sum.py").write_text("def total(values): return sum(values)\n")
    seed, _ = snapshot_checkpoint(source, service.directory / "seed", config.campaign.revision)
    return reopen_worker(service, config, seed)


def reopen_worker(
    service: RepositoryGoalService,
    config: RepositoryGoalConfig,
    seed: Path,
    manifest_sha256: str | None = None,
) -> CampaignGoalWorker:
    with service.harness.store.connect() as db:
        campaign = db.execute("SELECT id FROM campaigns").fetchone()["id"]
    return CampaignGoalWorker(
        service.harness,
        campaign,
        config.task,
        acceptance_contract_hash(config.task),
        version=2,
        seed=seed,
        seed_manifest_sha256=manifest_sha256
        or hashlib.sha256((seed.parent / "checkpoint.json").read_bytes()).hexdigest(),
    )


@pytest.mark.parametrize("interruption", ["before_seed", "during_seed"])
def test_seed_reconciles_after_enqueue_before_dispatch(
    audit_service: RepositoryGoalService,
    repository_config: RepositoryGoalConfig,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    worker = seeded_worker(audit_service, repository_config)
    assert worker.seed is not None
    expected = capture(worker.seed)[1]
    monkeypatch.setattr("loopforge.entrypoints.campaign_execution.preflight", no_preflight)

    def unexpected_dispatch(_key: str) -> None:
        pytest.fail("An interrupted seed must not reach dispatch")

    monkeypatch.setattr(worker, "_dispatch", unexpected_dispatch)
    with monkeypatch.context() as interrupted:
        if interruption == "before_seed":

            def fail_before_seed() -> None:
                message = "Injected process interruption before seeding"
                raise RuntimeError(message)

            interrupted.setattr(worker, "seed_candidate", fail_before_seed)
        else:
            original_replace = Path.replace

            def fail_during_seed(path: Path, target: Path) -> Path:
                if path.name == "sum.py.loopforge-seed-pending":
                    message = "Injected process interruption during seeding"
                    raise RuntimeError(message)
                return original_replace(path, target)

            interrupted.setattr(Path, "replace", fail_during_seed)
        with pytest.raises(RuntimeError, match="Injected process interruption"):
            worker.execute(GoalAction.DELEGATE, "milestone-intent")

    with audit_service.harness.store.connect() as db:
        queued = audit_service.harness.coding.jobs.state(db, worker.task)
    assert queued.status == "queued"
    assert queued.attempts == 0
    if interruption == "during_seed":
        partial = audit_service.harness.directory / "coding" / worker.task
        assert (partial / "src/parse.py").read_bytes() == expected["src/parse.py"][0]
        assert (partial / "src/sum.py").read_bytes() != expected["src/sum.py"][0]

    # A new bridge instance must reconcile solely from the durable operation/task state.
    restarted = reopen_worker(
        audit_service, repository_config, worker.seed, worker.seed_manifest_sha256
    )

    def check_dispatch(_key: str) -> None:
        root = audit_service.harness.directory / "coding" / restarted.task
        for name in ("src/parse.py", "src/sum.py"):
            body, mode = expected[name]
            assert (root / name).read_bytes() == body
            assert stat.S_IMODE((root / name).stat().st_mode) == mode
        assert not list(root.rglob("*.loopforge-seed-pending"))
        raise DispatchReached

    monkeypatch.setattr(restarted, "_dispatch", check_dispatch)
    with pytest.raises(DispatchReached):
        restarted.reconcile()


@pytest.mark.parametrize("tampering", ["bytes", "mode", "manifest", "head"])
def test_seed_tampering_denies_dispatch(
    audit_service: RepositoryGoalService,
    repository_config: RepositoryGoalConfig,
    monkeypatch: pytest.MonkeyPatch,
    tampering: str,
) -> None:
    worker = seeded_worker(audit_service, repository_config)
    assert worker.seed is not None
    monkeypatch.setattr("loopforge.entrypoints.campaign_execution.preflight", no_preflight)
    path = worker.seed / "src/parse.py"
    if tampering == "bytes":
        path.write_text("unrecorded checkpoint content\n")
    elif tampering == "mode":
        path.chmod(0o644)
    elif tampering == "manifest":
        manifest_path = worker.seed.parent / "checkpoint.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source"] = "unrecorded-source"
        manifest_path.write_text(json.dumps(manifest))
    else:
        local_git(
            worker.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--allow-empty",
            "-m",
            "unrecorded seed revision",
        )

    def deny_dispatch(_key: str) -> None:
        pytest.fail("Tampered checkpoint reached worker dispatch")

    restarted = reopen_worker(
        audit_service, repository_config, worker.seed, worker.seed_manifest_sha256
    )
    monkeypatch.setattr(restarted, "_dispatch", deny_dispatch)
    with pytest.raises(ValueError, match=r"Seed (manifest|checkpoint) changed"):
        restarted.execute(GoalAction.DELEGATE, "tampered-seed-intent")


class PagingSupervisor:
    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        self.offset = 0
        self.calls = 0

    def prepare(self, state: GoalState) -> SupervisorRequest:
        assert state.policy is not None
        return SupervisorRequest("{}", 1, "offline-page")

    def decide(self, request: SupervisorRequest) -> SupervisorResponse:
        assert request.maximum_microusd >= 0
        self.calls += 1
        return SupervisorResponse(
            SupervisorDecision(
                GoalAction.RETRIEVE,
                "Read the next artifact page",
                "Use exact original evidence",
                inspection=InspectionRequest(offset_bytes=self.offset),
                evidence_id=self.artifact_id,
            ),
            0,
            "offline-page",
        )


def test_distinct_pages_survive_coordinator_while_repeated_page_stops(
    audit_service: RepositoryGoalService,
) -> None:
    worker = audit_service.worker()
    raw = 'é雪"' * 3000 + "TAIL_SENTINEL\n"
    artifact = worker.research.store.publish_worker(raw, "baseline", "b" * 64)
    supervisor = PagingSupervisor(artifact.record_id)
    coordinator = GoalCoordinator(
        audit_service.journal, supervisor, worker, audit_service.clock, worker.research
    )
    pages: list[str] = []
    while True:
        assert coordinator.step(GOAL).phase == "action"
        state = coordinator.step(GOAL)
        assert state.phase == "ready"
        assert state.stagnant_decisions == 0
        record = state.research[-1]
        pages.append(record.excerpt)
        if record.next_offset_bytes is None:
            break
        assert record.next_offset_bytes > supervisor.offset
        supervisor.offset = record.next_offset_bytes
    assert len(pages) > 5
    assert "".join(pages) == raw
    assert state.policy is not None
    for expected_stagnation in range(1, state.policy.max_stagnant_decisions + 1):
        assert coordinator.step(GOAL).phase == "action"
        state = coordinator.step(GOAL)
        assert state.stagnant_decisions == expected_stagnation
    calls = supervisor.calls
    stopped = coordinator.step(GOAL)
    assert stopped.phase == "blocked"
    assert "meaningful-progress" in stopped.summary
    assert supervisor.calls == calls


def test_quote_heavy_acceptance_options_are_bounded_and_fully_retrievable(
    tmp_path: Path, repository_config: RepositoryGoalConfig
) -> None:
    values = repository_config.model_dump(mode="json")
    probe = values["task"]["acceptance_probes"][0]
    values["task"]["acceptance_probes"] = [
        dict(
            probe,
            criterion_id=f"criterion{i}",
            name=f"check_acceptance_p{i}",
            description='"' * 1000,
        )
        for i in range(10)
    ]
    values["task"]["objective"] = "g" * 8000
    values["remote_data"] = "synthetic-fixture"
    config = RepositoryGoalConfig.model_validate_json(json.dumps(values))
    service = RepositoryGoalService(tmp_path / "quote-heavy")
    provider = GeminiSupervisor("offline-admission-key")
    try:
        state = service.create(config)
        worker = service.worker()
        provider.evidence_store = worker.research.store
        request = provider.prepare(state)
        assert len(request.body.encode()) <= MAX_REQUEST_BYTES
        context = json.loads(json.loads(request.body)["contents"][0]["parts"][0]["text"])
        options = context["authorized_investigation_options"]
        assert context["goal"] == config.task.objective
        assert options["read_scope"] == config.campaign.scope
        assert options["edit_scope"] == config.task.allowed_paths
        assert options["checkpoint"] == config.campaign.revision
        original = worker.research.store.original(options["full_policy_artifact_id"])
        assert state.policy is not None
        assert json.loads(original) == json.loads(state.policy.investigation_contract)
        # Follow the published reference through the actual authorized retrieval path.
        supervisor = PagingSupervisor(options["full_policy_artifact_id"])
        coordinator = GoalCoordinator(
            service.journal, supervisor, worker, service.clock, worker.research
        )
        pages: list[str] = []
        while True:
            assert coordinator.step(GOAL).phase == "action"
            state = coordinator.step(GOAL)
            assert state.phase == "ready"
            record = state.research[-1]
            pages.append(record.excerpt)
            if record.next_offset_bytes is None:
                break
            supervisor.offset = record.next_offset_bytes
        assert "".join(pages) == original
    finally:
        provider.close()
        service.close()
