"""Prepared durable goal CLI: intake, unattended run, status and cancellation."""

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from loopforge.adapters.gemini_supervisor import GeminiSupervisor, load_gemini_key
from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.adapters.paid_budget import SQLitePaidBudget
from loopforge.adapters.research_store import ResearchStore
from loopforge.application.paid_supervisor import BudgetedSupervisor
from loopforge.application.supervisor import GoalCoordinator
from loopforge.domain.goals import CallFailed, GoalCreated, GoalPolicy, GoalState, GoalStopped
from loopforge.entrypoints.campaign import CampaignHarness
from loopforge.entrypoints.campaign_coding import acceptance_contract_hash
from loopforge.entrypoints.campaign_models import CampaignConfig, CodingTaskConfig, StrictRecord
from loopforge.entrypoints.goal_budget import paid_root
from loopforge.entrypoints.goal_guidance import worker_guidance
from loopforge.entrypoints.goal_worker import CampaignGoalWorker
from loopforge.ports.clock import ClockPort
from loopforge.ports.supervisor import ManagedGoalWorkerPort, SupervisorPort

GOAL = "prepared-goal"


class PreparedGoalConfig(StrictRecord):
    campaign: CampaignConfig
    task: CodingTaskConfig
    max_cost_microusd: int = Field(ge=1, le=5_000_000)
    max_goal_seconds: int = Field(default=3600, ge=30, le=86400)
    max_decisions: int = Field(default=12, ge=1, le=100)
    max_failures: int = Field(default=3, ge=1, le=10)
    # Opt-in: count only consecutive supervisor failures, reset by a valid decision.
    consecutive_failures: bool = False
    remote_data: Literal["none", "synthetic-fixture", "prepared-repository"] = "none"

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast("dict[str, object]", handler(self))
        # Omit the legacy default so frozen intake bindings are unchanged.
        if not self.consecutive_failures:
            data.pop("consecutive_failures", None)
        return data

    @model_validator(mode="after")
    def frontier_review_requires_remote_authority(self) -> "PreparedGoalConfig":
        if self.campaign.review_provider == "gemini" and self.remote_data == "none":
            msg = "Frontier review requires explicit remote data authority"
            raise ValueError(msg)
        return self


class SystemGoalClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class BaseGoalService:
    """One prepared goal per directory; budget and history survive every restart.

    This deliberately small Phase 2 composition never mutates the production
    campaign ledger, accepts changes, or runs mandatory generic review lanes.
    """

    def __init__(self, directory: Path) -> None:
        directory = directory.resolve()
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.journal = SQLiteGoalJournal(directory / "goals.sqlite")
        self.harness = CampaignHarness(directory / "worker", goal_features=True)
        try:
            self.harness.acquire_service()
        except BaseException:
            self.harness.close()
            raise
        self.clock: ClockPort = SystemGoalClock()

    def paid_budget(self) -> SQLitePaidBudget:
        state = self.journal.load(GOAL)
        assert state.policy is not None

        directory, root_state = paid_root(self.directory)
        assert root_state.policy is not None
        path = directory / "paid-budget.sqlite"
        budget = SQLitePaidBudget(path, root_state.policy.max_cost_microusd)
        budget.bootstrap(root_state.spent_microusd, root_state.reserved_microusd)
        self.harness.coding.parent_budget = budget
        return budget

    def close(self) -> None:
        self.harness.close()

    def worker(self) -> ManagedGoalWorkerPort:
        raise NotImplementedError

    def cancel(self) -> GoalState:
        state = self.journal.load(GOAL)
        if state.terminal:
            return state
        if state.phase == "inference":
            state = self.journal.append(
                GOAL,
                state.sequence,
                CallFailed(
                    state.reserved_microusd, "Cancelled unknown inference; maximum charged", 0
                ),
            )
        # Service ownership excludes another active coordinator. Reconcile any
        # interrupted worker; do not create a successor while cancelling.
        self.worker().recover_interrupted()
        return self.journal.append(
            GOAL,
            state.sequence,
            GoalStopped("cancelled", "Cancelled by operator; candidate work retained"),
        )


class PreparedGoalService(BaseGoalService):
    def create(self, config: PreparedGoalConfig) -> GoalState:
        if not config.task.acceptance_probes or not config.task.required_changed_paths:
            msg = "Prepared goals require trusted acceptance and explicit deliverables"
            raise ValueError(msg)
        path = self.directory / "intake.json"
        if path.exists():
            if PreparedGoalConfig.model_validate_json(path.read_text()) != config:
                msg = "Goal intake is immutable; use another goal directory"
                raise ValueError(msg)
        else:
            pending = self.directory / "intake.pending"
            with pending.open("w") as stream:
                stream.write(config.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(path)
        # Reconcile intake interrupted before its campaign or goal event existed.
        with self.harness.store.connect() as db:
            rows = db.execute("SELECT id,config FROM campaigns").fetchall()
        if not rows:
            self.harness.create(config.campaign)
        elif (
            len(rows) != 1
            or CampaignConfig.model_validate_json(rows[0]["config"]) != config.campaign
        ):
            msg = "Prepared goal worker ledger does not match intake"
            raise ValueError(msg)
        state = self.journal.load(GOAL)
        if state.phase == "new":
            state = self.journal.append(
                GOAL,
                0,
                GoalCreated(
                    GoalPolicy(
                        objective=config.task.objective,
                        contract_sha256=acceptance_contract_hash(config.task),
                        max_cost_microusd=config.max_cost_microusd,
                        deadline_epoch=int(self.clock.now().timestamp()) + config.max_goal_seconds,
                        max_decisions=config.max_decisions,
                        max_failures=config.max_failures,
                        consecutive_failures=config.consecutive_failures,
                        remote_data=config.remote_data,
                        worker_binding_sha256=hashlib.sha256(
                            config.model_dump_json().encode()
                        ).hexdigest(),
                    )
                ),
            )
        self.worker()  # validate the binding even on idempotent intake
        return state

    def worker(self) -> CampaignGoalWorker:
        config = PreparedGoalConfig.model_validate_json(
            (self.directory / "intake.json").read_text()
        )
        state = self.journal.load(GOAL)
        if state.policy is None or state.policy.contract_sha256 != acceptance_contract_hash(
            config.task
        ):
            msg = "Goal contract differs from frozen authority"
            raise ValueError(msg)
        if (
            state.policy.worker_binding_sha256
            != hashlib.sha256(config.model_dump_json().encode()).hexdigest()
        ):
            msg = "Goal intake binding differs from frozen authority"
            raise ValueError(msg)
        self.harness.coding.execution_deadline_epoch = state.policy.deadline_epoch
        if (
            state.policy.max_cost_microusd != config.max_cost_microusd
            or state.policy.remote_data != config.remote_data
        ):
            msg = "Goal policy differs from frozen authority"
            raise ValueError(msg)
        with self.harness.store.connect() as db:
            rows = db.execute("SELECT id,config FROM campaigns").fetchall()
        if (
            len(rows) != 1
            or CampaignConfig.model_validate_json(rows[0]["config"]) != config.campaign
        ):
            msg = "Goal campaign differs from frozen authority"
            raise ValueError(msg)
        worker = CampaignGoalWorker(
            self.harness,
            rows[0]["id"],
            config.task,
            state.policy.contract_sha256,
            guidance=lambda: worker_guidance(self.journal.load(GOAL)),
        )
        self.harness.coding.assignment_guidance[worker.task] = worker_guidance(state)
        return worker

    def coordinator(self, supervisor: SupervisorPort) -> GoalCoordinator:
        if isinstance(supervisor, GeminiSupervisor):
            supervisor.evidence_store = ResearchStore(self.journal)
        return GoalCoordinator(
            self.journal,
            BudgetedSupervisor(supervisor, self.paid_budget()),
            self.worker(),
            self.clock,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "create",
            "run",
            "status",
            "cancel",
            "pause",
            "resume",
            "report",
            "accept",
            "discard",
        ),
    )
    parser.add_argument("--directory", type=Path, default=Path(".loopforge/supervisor"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    if args.command in {"pause", "resume", "report", "accept", "discard"}:
        from loopforge.entrypoints.goal_review import review_command  # noqa: PLC0415

        print(json.dumps(review_command(args.directory, args.command), indent=2))
        return
    if args.command == "status":
        state = SQLiteGoalJournal(args.directory / "goals.sqlite").load(GOAL)
        print(json.dumps(asdict(state), indent=2))
        return
    service = PreparedGoalService(args.directory)
    supervisor = None
    try:
        if args.command == "create":
            if args.config is None:
                parser.error("create requires --config with an operator-prepared contract")
            state = service.create(PreparedGoalConfig.model_validate_json(args.config.read_text()))
        elif args.command == "cancel":
            state = service.cancel()
        else:
            supervisor = GeminiSupervisor(load_gemini_key(args.key_file))
            coordinator = service.coordinator(supervisor)
            state = service.journal.load(GOAL)
            while not state.terminal:
                state = coordinator.step(GOAL)
                print(
                    json.dumps(
                        {
                            "phase": state.phase,
                            "spent_microusd": state.spent_microusd,
                            "reserved_microusd": state.reserved_microusd,
                            "summary": state.summary,
                        }
                    ),
                    flush=True,
                )
                if not state.terminal:
                    time.sleep(1)
        print(json.dumps(asdict(state), indent=2))
    finally:
        if supervisor is not None:
            supervisor.close()
        service.close()


if __name__ == "__main__":
    main()
