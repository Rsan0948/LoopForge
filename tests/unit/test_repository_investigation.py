"""Hermetic investigation policy, evidence memory and preparation boundaries."""

import json
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from loopforge.adapters.container_sandbox import ContainerSandbox
from loopforge.adapters.gemini_supervisor import (
    MAX_REQUEST_BYTES,
    DecisionPayload,
    GeminiSupervisor,
)
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.goal_journal import SQLiteGoalJournal, decode_goal_event, encode_goal_event
from loopforge.domain.goals import (
    ActionObserved,
    CallReserved,
    DecisionRecorded,
    GoalAction,
    GoalState,
    ResearchRecorded,
    SupervisorDecision,
    WorkerObservation,
    goal_allowed_actions,
)
from loopforge.domain.investigation import FailureKind, InspectionRequest, TaskAssignment
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints import goal_investigation
from loopforge.entrypoints.campaign_coding import local_git, patch_identity
from loopforge.entrypoints.campaign_models import (
    AcceptanceProbe,
    CampaignConfig,
    CodingCheck,
    CodingTaskConfig,
)
from loopforge.entrypoints.repository_goal_models import ApprovedEnvironment, RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL
from loopforge.ports.sandbox import SandboxCommandResult, SandboxError
from loopforge.ports.supervisor import SupervisorRequest, SupervisorResponse


@pytest.fixture
def config(tmp_path: Path) -> RepositoryGoalConfig:
    workspace = GitWorkspaceManager(tmp_path / "source").materialize(
        FixtureSpec(
            fixture_id="research-policy",
            files=(
                FixtureFile(path="src/parse.py", content="def parse(value): return value\n"),
                FixtureFile(path="src/sum.py", content="def total(values): return 0\n"),
                FixtureFile(path="src/.env", content="PRIVATE=excluded\n"),
                FixtureFile(path="private/secret.py", content="PRIVATE='excluded'\n"),
                FixtureFile(
                    path="src/long.py", content="".join(f"value_{i}={i}\n" for i in range(200))
                ),
            ),
        )
    )
    check = CodingCheck(name="check_project", argv=["/usr/local/bin/python", "-B", "-c", "pass"])
    return RepositoryGoalConfig(
        campaign=CampaignConfig(
            repository=str(workspace.root),
            project="brook",
            revision=workspace.base_revision,
            scope=["src"],
            max_iterations=12,
            max_seconds=120,
        ),
        task=CodingTaskConfig(
            objective="Normalize quantities and compute their integer total",
            kind="bugfix",
            branch="goal/discovered",
            allowed_paths=["src"],
            image="sha256:" + "a" * 64,
            checks=[check],
            acceptance_probes=[
                AcceptanceProbe(
                    criterion_id="quantity",
                    description="Quantities normalize and total correctly",
                    name="check_acceptance_quantity",
                    source="assert 2 + 3 == 5",
                )
            ],
            evidence="Behavioral requirement",
            max_changed_files=3,
        ),
        environments=[
            ApprovedEnvironment(
                environment_id="stdlib",
                description="Prepared standard library environment",
                image="sha256:" + "a" * 64,
                readiness=[
                    CodingCheck(
                        name="check_environment",
                        argv=["/usr/local/bin/python", "-c", "import unittest"],
                    )
                ],
                checks=[check],
            )
        ],
        max_cost_microusd=500000,
        max_goal_seconds=300,
        max_decisions=24,
    )


@pytest.fixture
def service(
    tmp_path: Path, config: RepositoryGoalConfig
) -> Generator[RepositoryGoalService, None, None]:
    instance = RepositoryGoalService(tmp_path / "goal")
    try:
        instance.create(config)
        yield instance
    finally:
        instance.close()


def research(service: RepositoryGoalService, decision: SupervisorDecision) -> GoalState:
    state = service.journal.load(GOAL)
    key = f"research:{state.decisions}"
    state = service.journal.append(GOAL, state.sequence, CallReserved(key, 1))
    state = service.journal.append(GOAL, state.sequence, DecisionRecorded(decision, 0, "fixture"))
    record = service.worker().research.perform(decision, key, state)
    return service.journal.append(GOAL, state.sequence, ResearchRecorded(record))


def decision(action: GoalAction, **arguments: object) -> SupervisorDecision:
    payload = DecisionPayload.model_validate_json(
        json.dumps(
            {
                "action": action,
                "rationale": "Evidence-driven next step",
                "plan": "Complete the authorized behavioral goal",
                **arguments,
            }
        )
    )
    return SupervisorDecision(
        GoalAction(payload.action),
        payload.rationale,
        payload.plan,
        payload.inspection,
        payload.environment,
        payload.assignment,
        payload.evidence_id,
    )


def test_inventory_and_source_are_pinned_scoped_original_evidence(
    service: RepositoryGoalService, config: RepositoryGoalConfig
) -> None:
    inventory = research(service, decision(GoalAction.INSPECT, inspection={}))
    assert "parse.py" in inventory.research[-1].excerpt
    assert "PRIVATE" not in inventory.research[-1].excerpt
    (Path(config.campaign.repository) / "src/parse.py").write_text("uncommitted user text\n")
    state = research(service, decision(GoalAction.INSPECT, inspection={"path": "src/parse.py"}))
    record = state.research[-1]
    assert record.excerpt == "def parse(value): return value\n"
    assert record.revision == config.campaign.revision
    assert service.worker().research.store.original(record.record_id) == record.excerpt
    assert decode_goal_event(encode_goal_event(ResearchRecorded(record))) == ResearchRecorded(
        record
    )


@pytest.mark.parametrize("path", ["private/secret.py", "src/.env", "missing.py"])
def test_read_denial_cannot_expand_scope(service: RepositoryGoalService, path: str) -> None:
    state = service.journal.load(GOAL)
    with pytest.raises(ValueError, match=r"outside|safe relative"):
        service.worker().research.perform(
            decision(GoalAction.INSPECT, inspection={"path": path}), "denied", state
        )
    assert service.journal.load(GOAL) == state


def test_original_evidence_retrieval_and_range_omissions_survive_restart(
    service: RepositoryGoalService,
) -> None:
    state = research(
        service, decision(GoalAction.INSPECT, inspection={"path": "src/long.py", "line_count": 2})
    )
    source = state.research[-1]
    assert source.truncated
    assert "value_199" not in source.excerpt
    state = research(
        service,
        decision(
            GoalAction.RETRIEVE,
            evidence_id=source.record_id,
            inspection={"start_line": 199, "line_count": 2},
        ),
    )
    assert "value_199" in state.research[-1].excerpt
    assert state.research[-1].content_sha256 == source.content_sha256
    assert service.journal.load(GOAL) == state
    with pytest.raises(ValueError, match="owned"):
        service.worker().research.perform(
            decision(GoalAction.RETRIEVE, evidence_id="invented"), "invented", state
        )


def test_lost_research_ack_reuses_exact_receipt(service: RepositoryGoalService) -> None:
    state = service.journal.load(GOAL)
    action = decision(GoalAction.INSPECT, inspection={"path": "src/parse.py"})
    first = service.worker().research.perform(action, "stable", state)
    assert service.worker().research.perform(action, "stable", state) == first
    with pytest.raises(ValueError, match="different intent"):
        service.worker().research.perform(
            decision(GoalAction.INSPECT, inspection={"path": "src/sum.py"}), "stable", state
        )


def test_environment_and_baseline_gates_cannot_be_asserted_by_model(
    service: RepositoryGoalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert GoalAction.DELEGATE not in goal_allowed_actions(service.journal.load(GOAL))
    with pytest.raises(ValueError, match="catalog"):
        service.worker().research.perform(
            decision(GoalAction.PREPARE, environment="network-installer"),
            "bad-env",
            service.journal.load(GOAL),
        )

    def run(
        _self: ContainerSandbox, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        assert timeout_seconds is not None
        assert timeout_seconds <= 120
        return SandboxCommandResult(
            exit_code=0 if command_name == "check_environment" else 1,
            stdout="",
            stderr="independent baseline failure",
            succeeded=command_name == "check_environment",
        )

    monkeypatch.setattr(ContainerSandbox, "run", run)
    state = research(service, decision(GoalAction.PREPARE, environment="stdlib"))
    assert state.environment_ready
    assert not state.baseline_reproduced
    assert GoalAction.DELEGATE not in goal_allowed_actions(state)
    state = research(service, decision(GoalAction.REPRODUCE))
    assert state.baseline_reproduced
    assert state.research[-1].failure == FailureKind.VERIFICATION
    assert GoalAction.DELEGATE in goal_allowed_actions(state)


def test_unexecuted_or_early_exit_check_does_not_reproduce_behavior(
    service: RepositoryGoalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(
        _self: ContainerSandbox, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        return SandboxCommandResult(exit_code=0, stdout="", stderr="", succeeded=True)

    monkeypatch.setattr(ContainerSandbox, "run", run)
    state = research(service, decision(GoalAction.PREPARE, environment="stdlib"))
    state = research(service, decision(GoalAction.REPRODUCE))
    assert not state.baseline_reproduced
    assert state.research[-1].failure == FailureKind.ENVIRONMENT

    def unavailable(
        _self: ContainerSandbox, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        message = "injected sandbox failure"
        raise SandboxError(message)

    monkeypatch.setattr(ContainerSandbox, "run", unavailable)
    state = research(service, decision(GoalAction.PREPARE, environment="stdlib"))
    assert not state.environment_ready
    assert not state.baseline_reproduced
    assert '"executed": false' in state.research[-1].excerpt


def test_assignment_narrows_scope_without_changing_authority(
    service: RepositoryGoalService, monkeypatch: pytest.MonkeyPatch, config: RepositoryGoalConfig
) -> None:
    state = research(service, decision(GoalAction.INSPECT, inspection={"path": "src/parse.py"}))
    source_id = state.research[-1].record_id

    # Measurements are supplied by code-owned execution, not the proposal.
    def run(
        _self: ContainerSandbox, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        passed = command_name == "check_environment"
        return SandboxCommandResult(
            exit_code=0 if passed else 1, stdout="", stderr="", succeeded=passed
        )

    monkeypatch.setattr(ContainerSandbox, "run", run)
    research(service, decision(GoalAction.PREPARE, environment="stdlib"))
    state = research(service, decision(GoalAction.REPRODUCE))
    action = decision(
        GoalAction.DELEGATE,
        assignment={
            "objective": "Correct the discovered normalization and total functions",
            "files": ["src/parse.py", "src/sum.py"],
            "evidence_ids": [source_id],
        },
    )
    state = research(service, action)
    task = service.worker().research.task(state)
    assert task.allowed_paths == ["src/parse.py", "src/sum.py"]
    assert task.required_changed_paths == config.task.required_changed_paths
    assert task.required_changed_paths == []
    assert task.image == config.environments[0].image
    assert task.acceptance_probes == config.task.acceptance_probes
    assert task.max_task_tokens == config.task.max_task_tokens
    assert task.objective.startswith(config.task.objective)
    expanded = replace(
        state,
        assignment=TaskAssignment("Expand to private source", ("private/secret.py",), (source_id,)),
    )
    with pytest.raises(ValueError, match="expands"):
        service.worker().research.task(expanded)

    with pytest.raises(ValueError, match="idle"):
        service.worker().research.perform(
            decision(GoalAction.PREPARE, environment="stdlib"),
            "late-env",
            replace(state, observation=WorkerObservation("coding")),
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"permission": "shell"},
        {
            "assignment": {
                "objective": "Change any file",
                "files": ["src/a.py"],
                "evidence_ids": ["e_x"],
                "image": "evil",
            }
        },
    ],
)
def test_model_output_cannot_add_authority(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DecisionPayload.model_validate_json(
            json.dumps({"action": "delegate", "rationale": "reason", "plan": "plan", **extra})
        )


def test_scoped_directory_inventory_is_supported(service: RepositoryGoalService) -> None:
    state = research(service, decision(GoalAction.INSPECT, inspection={"path": "src"}))
    assert state.research[-1].kind == "inventory"
    assert "src/parse.py" in state.research[-1].excerpt
    assert "PRIVATE" not in state.research[-1].excerpt


def test_repeated_evidence_is_not_progress_even_when_queries_change(
    service: RepositoryGoalService,
) -> None:
    class NeverInfer:
        def prepare(self, state: GoalState) -> SupervisorRequest:
            assert state.policy is not None
            message = "Paid inference must not follow exhausted progress allowance"
            raise AssertionError(message)

        def decide(self, request: SupervisorRequest) -> SupervisorResponse:
            assert request.body
            message = "Paid inference must not occur"
            raise AssertionError(message)

    for query in ("absent-a", "absent-b", "absent-c", "absent-d", "absent-e"):
        state = research(
            service,
            decision(GoalAction.INSPECT, inspection={"path": "src/parse.py", "query": query}),
        )
    assert state.stagnant_decisions == 4
    stopped = service.coordinator(NeverInfer()).step(GOAL)
    assert stopped.phase == "blocked"
    assert "meaningful-progress" in stopped.summary


def test_source_queries_disclose_omitted_lines(service: RepositoryGoalService) -> None:
    state = research(
        service,
        decision(GoalAction.INSPECT, inspection={"path": "src/long.py", "query": "value_10"}),
    )
    record = state.research[-1]
    assert record.truncated
    assert "value_10" in record.excerpt
    assert "value_199" not in record.excerpt
    assert "value_199" in service.worker().research.store.original(record.record_id)


def test_raw_evidence_and_receipts_are_immutable(service: RepositoryGoalService) -> None:
    state = research(service, decision(GoalAction.INSPECT, inspection={"path": "src/parse.py"}))
    with service.journal.connect() as db, pytest.raises(Exception, match="immutable evidence"):
        db.execute("DELETE FROM research_artifacts")
    with service.journal.connect() as db, pytest.raises(Exception, match="immutable receipt"):
        db.execute("UPDATE research_receipts SET intent='downgrade'")
    record = state.research[-1]
    assert service.worker().research.store.original(record.record_id) == record.excerpt


def test_record_cannot_claim_wrong_checkpoint_or_environment(
    service: RepositoryGoalService,
) -> None:
    state = service.journal.load(GOAL)
    action = decision(GoalAction.INSPECT, inspection={"path": "src/parse.py"})
    state = service.journal.append(GOAL, state.sequence, CallReserved("key", 1))
    state = service.journal.append(GOAL, state.sequence, DecisionRecorded(action, 0, "fixture"))
    record = service.worker().research.perform(action, "key", state)
    with pytest.raises(ValueError, match="checkpoint"):
        service.journal.append(
            GOAL, state.sequence, ResearchRecorded(replace(record, revision="f" * 40))
        )
    with pytest.raises(ValueError, match="approved proposed authority"):
        service.journal.append(
            GOAL,
            state.sequence,
            ResearchRecorded(replace(record, kind="environment", environment_id="evil")),
        )


@pytest.mark.parametrize("payload", [b"binary\x00source", b"x" * 262145])
def test_unsupported_source_fails_before_artifact_admission(
    service: RepositoryGoalService, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    original = goal_investigation.local_git_bytes

    def source(root: Path, *arguments: str) -> bytes:
        if arguments[0] == "show":
            return payload
        return original(root, *arguments)

    monkeypatch.setattr(goal_investigation, "local_git_bytes", source)
    with pytest.raises(ValueError, match="supported text"):
        service.worker().research.perform(
            decision(GoalAction.INSPECT, inspection={"path": "src/parse.py"}),
            "oversized",
            service.journal.load(GOAL),
        )
    with service.journal.connect() as db:
        assert db.execute("SELECT count(*) FROM research_artifacts").fetchone()[0] == 0


def test_pinned_symlink_cannot_be_read_as_source(
    tmp_path: Path, config: RepositoryGoalConfig
) -> None:
    repository = Path(config.campaign.repository)
    (repository / "src/link.py").symlink_to("../private/secret.py")
    local_git(repository, "add", "src/link.py")
    local_git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@invalid",
        "commit",
        "-m",
        "symlink fixture",
    )
    revision = local_git(repository, "rev-parse", "HEAD")
    config = config.model_copy(
        update={"campaign": config.campaign.model_copy(update={"revision": revision})}
    )
    service = RepositoryGoalService(tmp_path / "symlink-goal")
    try:
        service.create(config)
        with pytest.raises(ValueError, match="regular source"):
            service.worker().research.perform(
                decision(GoalAction.INSPECT, inspection={"path": "src/link.py"}),
                "symlink",
                service.journal.load(GOAL),
            )
    finally:
        service.close()


def test_a02_current_candidate_inspection_is_identity_bound(
    service: RepositoryGoalService, config: RepositoryGoalConfig
) -> None:

    root = Path(config.campaign.repository)
    workspace = GitWorkspaceManager(root.parent).adopt_existing(root)
    (root / "src/parse.py").write_text("def parse(value): return int(value)\n")
    identity = patch_identity(workspace)
    state = service.journal.load(GOAL)
    service.journal.append(
        GOAL,
        state.sequence,
        ActionObserved(WorkerObservation("attention", candidate_sha256=identity)),
    )
    investigator = service.worker().research
    investigator.candidate = lambda: (root, patch_identity(workspace))
    action = decision(
        GoalAction.INSPECT,
        inspection={"path": "src/parse.py", "target": "candidate", "candidate_sha256": identity},
    )
    state = service.journal.load(GOAL)
    record = investigator.perform(action, "current", state)
    assert "int(value)" in record.excerpt
    assert record.candidate_sha256 == identity
    baseline = investigator.perform(
        decision(GoalAction.INSPECT, inspection={"path": "src/parse.py"}), "baseline", state
    )
    assert "int(value)" not in baseline.excerpt
    (root / "src/parse.py").write_text("changed again\n")
    with pytest.raises(ValueError, match="stale"):
        investigator.perform(action, "stale", state)


def test_a03_a04_worker_artifact_utf8_tail_is_losslessly_retrievable(
    service: RepositoryGoalService,
) -> None:

    store = service.worker().research.store
    raw = 'é雪"' * 4000 + "TAIL_SENTINEL\n"
    artifact = store.publish_worker(raw, "baseline", "b" * 64)
    offset = 0
    pages: list[str] = []
    while True:
        state = research(
            service,
            decision(
                GoalAction.RETRIEVE,
                evidence_id=artifact.record_id,
                inspection={"offset_bytes": offset},
            ),
        )
        record = state.research[-1]
        pages.append(record.excerpt)
        assert len(record.excerpt.encode()) <= 2000
        if record.next_offset_bytes is None:
            break
        assert record.next_offset_bytes > offset
        offset = record.next_offset_bytes
    assert "".join(pages) == raw
    with pytest.raises(ValueError, match="UTF-8"):
        service.worker().research.perform(
            SupervisorDecision(
                GoalAction.RETRIEVE,
                "Read original",
                "Continue paging",
                inspection=InspectionRequest(offset_bytes=1),
                evidence_id=artifact.record_id,
            ),
            "unaligned",
            state,
        )
    with pytest.raises(ValueError, match="owned"):
        service.worker().research.perform(
            decision(GoalAction.RETRIEVE, evidence_id="foreign"), "foreign", state
        )


@pytest.mark.parametrize(
    ("intent", "expected"), [("feature", True), ("launch", True), ("bugfix", False)]
)
def test_a06_passing_baseline_is_measured_for_creation(
    tmp_path: Path,
    config: RepositoryGoalConfig,
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    expected: bool,
) -> None:
    raw = config.model_dump(mode="json")
    raw["task"]["kind"] = intent
    instance = RepositoryGoalService(tmp_path / "creation")
    try:
        instance.create(RepositoryGoalConfig.model_validate_json(json.dumps(raw)))
        marker = config.task.acceptance_probes[0].completion_marker

        def healthy_run(
            _self: ContainerSandbox, name: str, **kwargs: object
        ) -> SandboxCommandResult:
            return SandboxCommandResult(
                exit_code=0,
                stdout=marker if name.startswith("check_acceptance") else "",
                stderr="",
                succeeded=True,
            )

        monkeypatch.setattr(ContainerSandbox, "run", healthy_run)
        research(instance, decision(GoalAction.PREPARE, environment="stdlib"))
        state = research(instance, decision(GoalAction.REPRODUCE))
        assert state.baseline_reproduced is expected
        assert (GoalAction.DELEGATE in goal_allowed_actions(state)) is expected
    finally:
        instance.close()


def test_a05_quote_heavy_context_packs_to_retrievable_artifact(
    service: RepositoryGoalService,
) -> None:

    state = service.journal.load(GOAL)
    assert state.policy
    raw = json.dumps({"patch": '"' * 11000})
    state = replace(
        state,
        policy=replace(state.policy, objective="g" * 8000, remote_data="synthetic-fixture"),
        plan="p" * 4000,
        observation=WorkerObservation("attention", raw, "b" * 64),
    )
    provider = GeminiSupervisor("offline-test-key")
    try:
        provider.evidence_store = service.worker().research.store
        request = provider.prepare(state)
        assert len(request.body.encode()) <= MAX_REQUEST_BYTES
        context = json.loads(json.loads(request.body)["contents"][0]["parts"][0]["text"])
        ids = context["worker_evidence_untrusted"]["artifact_ids"]
        assert ids
        assert provider.evidence_store.original(ids[0]) == raw
        assert context["worker_evidence_untrusted"]["candidate_sha256"] == "b" * 64
        assert context["goal"] == "g" * 8000
    finally:
        provider.close()


def test_a08_assignment_versions_preserve_candidate_and_scope(
    service: RepositoryGoalService, config: RepositoryGoalConfig, monkeypatch: pytest.MonkeyPatch
) -> None:

    state = research(service, decision(GoalAction.INSPECT, inspection={"path": "src/parse.py"}))
    source = state.research[-1].record_id

    def run(_self: ContainerSandbox, name: str, **kwargs: object) -> SandboxCommandResult:
        passed = name == "check_environment"
        return SandboxCommandResult(
            exit_code=0 if passed else 1, stdout="", stderr="", succeeded=passed
        )

    monkeypatch.setattr(ContainerSandbox, "run", run)
    research(service, decision(GoalAction.PREPARE, environment="stdlib"))
    research(service, decision(GoalAction.REPRODUCE))
    first = {
        "objective": "Repair normalization from source evidence",
        "files": ["src/parse.py"],
        "evidence_ids": [source],
    }
    state = research(service, decision(GoalAction.DELEGATE, assignment=first))
    root = Path(config.campaign.repository)
    workspace = GitWorkspaceManager(root.parent).adopt_existing(root)
    (root / "src/parse.py").write_text("def parse(value): return int(value)\n")
    identity = patch_identity(workspace)
    state = service.journal.append(
        GOAL,
        state.sequence,
        ActionObserved(WorkerObservation("attention", candidate_sha256=identity)),
    )
    investigation = service.worker().research
    investigation.candidate = lambda: (root, patch_identity(workspace))
    action = decision(
        GoalAction.DELEGATE,
        assignment={
            "objective": "Implement the next total milestone",
            "files": ["src/sum.py"],
            "evidence_ids": [source],
        },
    )
    key = "revision"
    state = service.journal.append(GOAL, state.sequence, CallReserved(key, 1))
    state = service.journal.append(GOAL, state.sequence, DecisionRecorded(action, 0, "fixture"))
    record = investigation.perform(action, key, state)
    state = service.journal.append(GOAL, state.sequence, ResearchRecorded(record))
    assert len(state.assignment_versions) == 2
    checkpoint = service.directory / state.assignment_versions[-1].checkpoint / "checkpoint"
    assert (checkpoint / "src/parse.py").read_text() == "def parse(value): return int(value)\n"
    assert set(service.worker().research.task(state).allowed_paths) == {
        "src/parse.py",
        "src/sum.py",
    }
    assert SQLiteGoalJournal(service.directory / "goals.sqlite").load(GOAL) == state


def test_a01_mistyped_scoped_path_can_be_corrected_without_executing_outside_scope(
    service: RepositoryGoalService,
) -> None:
    class CorrectingSupervisor:
        calls = 0

        def prepare(self, state: GoalState) -> SupervisorRequest:
            del state
            return SupervisorRequest("{}", 1, "fixture")

        def decide(self, request: SupervisorRequest) -> SupervisorResponse:
            del request
            self.calls += 1
            return SupervisorResponse(
                decision(
                    GoalAction.INSPECT,
                    inspection={"path": "src/typo.py" if self.calls == 1 else "src/parse.py"},
                ),
                0,
                "fixture",
            )

    coordinator = service.coordinator(CorrectingSupervisor())
    coordinator.step(GOAL)
    rejected = coordinator.step(GOAL)
    assert rejected.phase == "ready"
    assert rejected.action_rejections == 1
    assert "absent" in rejected.summary
    coordinator.step(GOAL)
    corrected = coordinator.step(GOAL)
    assert corrected.research[-1].kind == "source"
    assert "def parse" in corrected.research[-1].excerpt


def test_query_at_nonzero_offset_retains_valid_synthetic_evidence_range(
    service: RepositoryGoalService,
) -> None:
    state = research(
        service,
        decision(
            GoalAction.INSPECT,
            inspection={"path": "src/long.py", "start_line": 10, "query": "value_20"},
        ),
    )
    record = state.research[-1]
    assert "value_20" in record.excerpt
    assert record.start_offset_bytes == 0
    assert record.end_offset_bytes == 0


@pytest.mark.parametrize("open_files", [128, 1024])
def test_baseline_measurement_uses_the_frozen_coding_sandbox_limits(
    tmp_path: Path, config: RepositoryGoalConfig, monkeypatch: pytest.MonkeyPatch, open_files: int
) -> None:
    config = config.model_copy(
        update={"task": config.task.model_copy(update={"sandbox_max_open_files": open_files})}
    )
    observed: list[int] = []

    def run(
        self: ContainerSandbox, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        del timeout_seconds
        observed.append(self._limits.max_open_files)  # pyright: ignore[reportPrivateUsage]
        ok = command_name == "check_environment"
        return SandboxCommandResult(exit_code=0 if ok else 1, stdout="", stderr="x", succeeded=ok)

    monkeypatch.setattr(ContainerSandbox, "run", run)
    instance = RepositoryGoalService(tmp_path / "limits-goal")
    try:
        instance.create(config)
        research(instance, decision(GoalAction.PREPARE, environment="stdlib"))
        research(instance, decision(GoalAction.REPRODUCE))
    finally:
        instance.close()
    assert observed
    assert set(observed) == {open_files}


def test_baseline_excerpt_puts_failing_checks_first_and_keeps_the_full_record() -> None:
    from loopforge.entrypoints.goal_investigation import failures_first  # noqa: PLC0415

    raw = json.dumps(
        {
            "environment": "env",
            "revision": "r",
            "outcomes": [
                {"name": "check_documents", "passed": True, "stdout": "PDF text " * 500},
                {"name": "check_project", "passed": False, "stdout": "E  assert 422 == 409"},
            ],
        }
    )
    excerpt = failures_first(raw)
    assert excerpt.encode()[:2000].decode().count("assert 422 == 409") == 1
    assert "PDF text" not in excerpt
    data = json.loads(excerpt)
    assert [o["name"] for o in data["outcomes"]] == ["check_project", "check_documents"]
    assert data["outcomes"][1] == {"name": "check_documents", "passed": True}
    # Nothing is lost: the stored record is the unchanged raw output.
    assert json.loads(raw)["outcomes"][0]["stdout"].startswith("PDF text")
