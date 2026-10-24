"""Repository-goal composition over the durable coordinator and local worker."""

import json
import os
from pathlib import Path

from loopforge.adapters.research_store import ResearchStore
from loopforge.application.paid_supervisor import BudgetedSupervisor
from loopforge.application.supervisor import GoalCoordinator
from loopforge.domain.goals import GoalAction, GoalCreated, GoalPolicy, GoalState, WorkerObservation
from loopforge.entrypoints.campaign_coding import acceptance_contract_hash, patch_identity
from loopforge.entrypoints.campaign_models import CampaignConfig, CodingCommand, OperatorCommand
from loopforge.entrypoints.goal_checkpoint import snapshot_checkpoint
from loopforge.entrypoints.goal_guidance import worker_guidance
from loopforge.entrypoints.goal_investigation import RepositoryInvestigation
from loopforge.entrypoints.goal_worker import CampaignGoalWorker
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig, repository_binding
from loopforge.entrypoints.supervisor import GOAL, BaseGoalService
from loopforge.ports.supervisor import EvidenceAwareSupervisorPort, SupervisorPort


class RepositoryGoalWorker:
    def __init__(
        self, service: "RepositoryGoalService", research: RepositoryInvestigation, campaign: str
    ) -> None:
        self.service, self.research, self.campaign = service, research, campaign

    def prepared(self) -> CampaignGoalWorker | None:
        state = self.service.journal.load(GOAL)
        if state.assignment is None:
            return None
        task = self.research.task(state)
        worker = CampaignGoalWorker(
            self.service.harness,
            self.campaign,
            task,
            acceptance_contract_hash(task),
            version=max(1, len(state.assignment_versions)),
            seed_manifest_sha256=state.assignment_versions[-1].seed_manifest_sha256
            if state.assignment_versions
            else "",
            seed=(self.service.directory / state.assignment_versions[-1].checkpoint / "checkpoint")
            if state.assignment_versions and state.assignment_versions[-1].checkpoint
            else None,
        )
        self.service.harness.coding.assignment_guidance[worker.task] = worker_guidance(
            state, self.research.store.original
        )
        return worker

    def observe(self) -> WorkerObservation:
        worker = self.prepared()
        observation = worker.observe() if worker else WorkerObservation()
        if observation.verified and worker is not None:
            workspace = worker.workspace()
            snapshot_checkpoint(
                workspace.root,
                self.service.directory / "verified" / observation.candidate_sha256,
                workspace.base_revision,
            )
        return observation

    def execute(self, action: GoalAction, key: str) -> WorkerObservation:
        worker = self.prepared()
        if worker is None:
            msg = "Worker assignment is not ready"
            raise ValueError(msg)
        if action == GoalAction.DELEGATE:
            with self.service.harness.store.connect() as db:
                previous = [
                    str(t["id"])
                    for t in self.service.harness.coding.jobs.tasks(db, self.campaign)
                    if str(t["id"]) != worker.task
                    and self.service.harness.coding.jobs.state(db, str(t["id"])).status
                    not in {"accepted", "rejected"}
                ]
            for task in previous:
                self.service.harness.coding.command(
                    self.campaign,
                    task,
                    CodingCommand(
                        command="reject",
                        evidence="Preserved as predecessor of a versioned assignment",
                    ),
                )
            with self.service.harness.store.connect() as db:
                status = self.service.harness.store.state(db, self.campaign).status
            if status == "attention":
                self.service.harness.command(
                    self.campaign,
                    OperatorCommand(
                        command="resume",
                        evidence="Continue versioned assignment from preserved candidate",
                    ),
                )
        return worker.execute(action, key)

    def reconcile(self) -> WorkerObservation:
        worker = self.prepared()
        if worker is None:
            msg = "No prepared worker intent to reconcile"
            raise ValueError(msg)
        return worker.reconcile()

    def recover_interrupted(self) -> None:
        worker = self.prepared()
        if worker:
            worker.recover_interrupted()


class RepositoryGoalService(BaseGoalService):
    def create(self, config: RepositoryGoalConfig) -> GoalState:
        if config.capture_working_tree:
            original = self.directory / "operator-intake.json"
            if (
                original.exists()
                and RepositoryGoalConfig.model_validate_json(original.read_text()) != config
            ):
                msg = "Operator intake is immutable"
                raise ValueError(msg)
            original.write_text(config.model_dump_json())
            source, revision = snapshot_checkpoint(
                Path(config.campaign.repository), self.directory, config.campaign.revision
            )
            config = RepositoryGoalConfig.model_validate_json(
                json.dumps(
                    {
                        **config.model_dump(mode="json"),
                        "capture_working_tree": False,
                        "campaign": {
                            **config.campaign.model_dump(mode="json"),
                            "repository": str(source),
                            "revision": revision,
                        },
                    }
                )
            )
        # Intake is written atomically before any campaign or goal is visible.
        path = self.directory / "repository-intake.json"
        if path.exists():
            if RepositoryGoalConfig.model_validate_json(path.read_text()) != config:
                msg = "Repository goal intake is immutable"
                raise ValueError(msg)
        else:
            pending = path.with_suffix(".pending")
            with pending.open("w") as stream:
                stream.write(config.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(path)
        with self.harness.store.connect() as db:
            rows = db.execute("SELECT id,config FROM campaigns").fetchall()
        if not rows:
            self.harness.create(config.campaign)
        elif (
            len(rows) != 1
            or CampaignConfig.model_validate_json(rows[0]["config"]) != config.campaign
        ):
            msg = "Repository worker ledger differs from intake"
            raise ValueError(msg)
        state = self.journal.load(GOAL)
        if state.phase == "new":
            options = {
                "read_scope": config.campaign.scope,
                "edit_scope": config.task.allowed_paths,
                "checkpoint": config.campaign.revision,
                "acceptance_criteria": [
                    {"id": probe.criterion_id, "description": probe.description}
                    for probe in config.task.acceptance_probes
                ],
                "approved_environments": [
                    {
                        "id": environment.environment_id,
                        "description": environment.description,
                        "checks": [check.name for check in environment.checks],
                    }
                    for environment in config.environments
                ],
                "max_changed_files": config.task.max_changed_files,
                "workflow": (
                    "Inspect original source, prepare an approved environment, reproduce "
                    "behavior, then propose a narrowed assignment citing source evidence IDs. "
                    "Adapt order and retrieve evidence as needed; no generic review lanes."
                ),
            }
            state = self.journal.append(
                GOAL,
                0,
                GoalCreated(
                    GoalPolicy(
                        objective=config.task.objective,
                        task_intent=config.task.kind,
                        require_recovery_evidence=True,
                        contract_sha256=acceptance_contract_hash(config.task),
                        max_cost_microusd=config.max_cost_microusd,
                        deadline_epoch=int(self.clock.now().timestamp()) + config.max_goal_seconds,
                        max_decisions=config.max_decisions,
                        max_failures=config.max_failures,
                        consecutive_failures=config.consecutive_failures,
                        remote_data=config.remote_data,
                        worker_binding_sha256=repository_binding(config),
                        investigation_enabled=True,
                        investigation_contract=json.dumps(options),
                        max_research_failures=config.max_research_failures,
                        max_stagnant_decisions=config.max_stagnant_decisions,
                        approved_environment_ids=tuple(
                            environment.environment_id for environment in config.environments
                        ),
                        checkpoint_revision=config.campaign.revision,
                        clarifications=config.clarifications,
                        dynamic_clarifications=config.dynamic_clarifications,
                    )
                ),
            )
        self.worker()
        return state

    def worker(self) -> RepositoryGoalWorker:
        config = RepositoryGoalConfig.model_validate_json(
            (self.directory / "repository-intake.json").read_text()
        )
        state = self.journal.load(GOAL)
        if (
            state.policy is None
            or not state.policy.investigation_enabled
            or state.policy.worker_binding_sha256 != repository_binding(config)
        ):
            msg = "Repository policy differs from frozen authority"
            raise ValueError(msg)
        self.harness.coding.execution_deadline_epoch = state.policy.deadline_epoch
        with self.harness.store.connect() as db:
            rows = db.execute("SELECT id,config FROM campaigns").fetchall()
        if (
            len(rows) != 1
            or CampaignConfig.model_validate_json(rows[0]["config"]) != config.campaign
        ):
            msg = "Repository campaign differs from frozen authority"
            raise ValueError(msg)

        def candidate() -> tuple[Path, str]:
            prepared = self.worker().prepared()
            if prepared is None:
                msg_0 = "No candidate is available"
                raise ValueError(msg_0)
            workspace = prepared.workspace()
            return workspace.root, patch_identity(workspace)

        research = RepositoryInvestigation(
            config, self.directory, ResearchStore(self.journal), candidate
        )
        return RepositoryGoalWorker(self, research, rows[0]["id"])

    def coordinator(self, supervisor: SupervisorPort) -> GoalCoordinator:
        worker = self.worker()
        if isinstance(supervisor, EvidenceAwareSupervisorPort):
            supervisor.attach_evidence(ResearchStore(self.journal))
        return GoalCoordinator(
            self.journal,
            BudgetedSupervisor(supervisor, self.paid_budget()),
            worker,
            self.clock,
            worker.research,
        )


if __name__ == "__main__":
    # Documented entry point; the command line itself lives in goal_commands.
    from loopforge.entrypoints.goal_commands import repository_main

    repository_main()
