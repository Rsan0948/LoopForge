"""Explicit live qualification with frozen repetitions, cash allocations and outcomes.

Deterministic tests exercise this runner but never certify real model autonomy.
No inference is started unless the operator supplies --live and a frozen manifest.
"""

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, model_validator

from loopforge.adapters.acceptance_receipts import decode_acceptance
from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.research_store import ResearchStore
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.application.failure_recovery import worker_failure
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import (
    ActionProposed,
    ModelAttemptFailed,
    ToolSucceeded,
    VerificationFailed,
)
from loopforge.domain.goals import GoalState
from loopforge.domain.investigation import FailureKind
from loopforge.domain.types import ActionId
from loopforge.entrypoints.campaign_coding import local_git_bytes
from loopforge.entrypoints.campaign_models import StrictRecord
from loopforge.entrypoints.goal_bootstrap import GoalWiring, wire
from loopforge.entrypoints.goal_review import morning_report
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL


class QualificationCase(StrictRecord):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,60}$")
    scenario: Literal["wrong-hypothesis", "healthy-feature", "scaffold-creation", "multistage"]
    intake: RepositoryGoalConfig
    wrong_hypothesis_path: str | None = None
    milestone_criteria: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def scenario_contract(self) -> "QualificationCase":
        if self.scenario == "wrong-hypothesis" and (
            self.intake.task.kind != "bugfix" or not self.wrong_hypothesis_path
        ):
            msg = "Wrong-hypothesis trials require a bug and a frozen wrong-edit target"
            raise ValueError(msg)
        if (
            self.scenario in {"healthy-feature", "multistage"}
            and self.intake.task.kind != "feature"
        ):
            msg = "Feature qualification requires a feature workload"
            raise ValueError(msg)
        if self.scenario == "scaffold-creation" and self.intake.task.kind != "launch":
            msg = "Scaffold qualification requires an explicit creation workload"
            raise ValueError(msg)
        ids = {p.criterion_id for p in self.intake.task.acceptance_probes}
        if self.scenario == "multistage" and (
            len(set(self.milestone_criteria)) < 2 or not set(self.milestone_criteria) <= ids
        ):
            msg = "Multistage trials require at least two independent milestone criteria"
            raise ValueError(msg)
        return self


class QualificationPlan(StrictRecord):
    cases: list[QualificationCase] = Field(min_length=4, max_length=12)
    repetitions: int = Field(default=2, ge=2, le=5)
    paid_cap_microusd: int = Field(ge=1, le=5_000_000)
    minimum_completion_rate: float = Field(default=1.0, gt=0, le=1)
    maximum_interventions: int = Field(default=0, ge=0, le=5)
    maximum_protocol_failures: int = Field(default=0, ge=0, le=10)

    @model_validator(mode="after")
    def allocation(self) -> "QualificationPlan":
        if len({c.name for c in self.cases}) != len(self.cases):
            msg = "Case names must be unique"
            raise ValueError(msg)
        if {c.scenario for c in self.cases} != {
            "wrong-hypothesis",
            "healthy-feature",
            "scaffold-creation",
            "multistage",
        }:
            msg = "All four real-model qualification scenarios are required"
            raise ValueError(msg)
        if (
            sum(c.intake.max_cost_microusd for c in self.cases) * self.repetitions
            > self.paid_cap_microusd
        ):
            msg = "All-role trial allocations exceed the frozen qualification cap"
            raise ValueError(msg)
        shapes = {
            tuple(
                local_git_bytes(
                    Path(c.intake.campaign.repository),
                    "ls-tree",
                    "-r",
                    "--name-only",
                    c.intake.campaign.revision,
                )
                .decode()
                .splitlines()
            )
            for c in self.cases
        }
        if len(shapes) < 2:
            msg_0 = "Qualification requires more than one repository shape"
            raise ValueError(msg_0)
        return self


def trial_metrics(root: Path, state: GoalState, case: QualificationCase) -> dict[str, object]:  # noqa: PLR0912, PLR0915 - explicit boundary dispatch/normalization
    with sqlite3.connect(root / "goals.sqlite") as db:
        interventions = sum(
            json.loads(row[0])["kind"] in {"question_answered", "control"}
            for row in db.execute("SELECT event FROM goal_events")
        )
    protocols = state.action_rejections
    wrong_failed = False
    later_different_edit = False
    runtime_path = root / "worker/runs.sqlite"
    if runtime_path.exists():
        store = SQLiteEventStore(runtime_path, codec=JsonEventCodec())
        for run in sorted(store.list_runs(), key=lambda r: str(r.run_id)):
            proposals: dict[ActionId, ActionProposal] = {}
            last_edit = ""
            for event in store.events_for(run.run_id):
                if isinstance(event, ActionProposed):
                    proposals[event.proposal.action_id] = event.proposal
                if isinstance(event, ToolSucceeded):
                    proposal = proposals.get(event.action_id)
                    if proposal and proposal.tool_name in {
                        "apply_edits",
                        "edit_file",
                        "write_file",
                    }:
                        last_edit = proposal.arguments.get("path", "")
                        if wrong_failed and last_edit != case.wrong_hypothesis_path:
                            later_different_edit = True
                if (
                    isinstance(event, VerificationFailed)
                    and last_edit == case.wrong_hypothesis_path
                ):
                    wrong_failed = True
                if (
                    isinstance(event, ModelAttemptFailed)
                    and worker_failure([event]) == FailureKind.PROTOCOL
                ):
                    protocols += 1
    baseline_passed: set[str] = set()
    for record in state.research:
        if record.kind == "baseline":
            raw = ResearchStore(SQLiteGoalJournal(root / "goals.sqlite")).original(record.record_id)
            baseline_passed = {o["name"] for o in json.loads(raw)["outcomes"] if o["passed"]}
    passed_criteria: list[set[str]] = []
    database = root / "worker/campaigns.sqlite"
    if database.exists():
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            for (raw,) in db.execute("SELECT detail FROM coding_events ORDER BY rowid"):
                detail = json.loads(raw)
                if "acceptance_receipt" not in detail:
                    continue
                try:
                    receipt = decode_acceptance(json.dumps(detail["acceptance_receipt"]))
                except ValueError:
                    continue
                passed_criteria.append(
                    {c.criterion_id for c in receipt.criteria if c.executed and c.passed}
                )
    baseline_ids = {
        p.criterion_id for p in case.intake.task.acceptance_probes if p.name in baseline_passed
    }
    new_criteria = (
        set[str]().union(*passed_criteria) - baseline_ids if passed_criteria else set[str]()
    )
    kind = case.scenario
    source_files = (
        local_git_bytes(
            Path(case.intake.campaign.repository),
            "ls-tree",
            "-r",
            "--name-only",
            case.intake.campaign.revision,
        )
        .decode()
        .splitlines()
    )
    if kind == "wrong-hypothesis":
        evidence = wrong_failed and later_different_edit
    elif kind == "healthy-feature":
        evidence = all(c.name in baseline_passed for c in case.intake.task.checks)
    elif kind == "scaffold-creation":
        evidence = len(source_files) <= 12 and bool(new_criteria)
    else:
        milestones = set(case.milestone_criteria)
        evidence = (
            len(state.assignment_versions) >= 2
            and any(0 < len(p & milestones) < len(milestones) for p in passed_criteria)
            and any(milestones <= p for p in passed_criteria)
        )
    return {
        "interventions": interventions,
        "protocol_rejections": protocols,
        "scenario_evidence": evidence,
        "useful_partial_progress": bool(new_criteria) and state.phase != "delivered",
        "newly_passing_criteria": sorted(new_criteria),
    }


def qualification_passed(plan: QualificationPlan, results: list[dict[str, object]]) -> bool:
    return all(
        len(rows := [r for r in results if r["case"] == case.name]) == plan.repetitions
        and len({r["repeat"] for r in rows}) == plan.repetitions
        and sum(
            bool(r["complete"])
            and bool(r["scenario_evidence"])
            and int(cast("int", r["interventions"])) <= plan.maximum_interventions
            and int(cast("int", r["protocol_rejections"])) <= plan.maximum_protocol_failures
            for r in rows
        )
        / plan.repetitions
        >= plan.minimum_completion_rate
        for case in plan.cases
    )


def qualify(plan: QualificationPlan, directory: Path, key_file: Path) -> dict[str, object]:
    if directory.exists():
        msg = "Live qualification requires a fresh artifact directory"
        raise ValueError(msg)
    directory.mkdir(parents=True)
    manifest = plan.model_dump_json(indent=2)
    (directory / "plan.json").write_text(manifest)
    (directory / "plan.sha256").write_text(hashlib.sha256(manifest.encode()).hexdigest())
    results: list[dict[str, object]] = []
    for case in plan.cases:
        for repeat in range(plan.repetitions):
            root = directory / f"{case.name}-{repeat + 1}"
            service = RepositoryGoalService(root)
            stack = None
            started = time.monotonic()
            try:
                state = service.create(case.intake)
                timed_out = False
                stack = wire(service, GoalWiring(key_file=key_file))
                coordinator = stack.coordinator
                while not state.terminal:
                    if time.monotonic() - started > case.intake.max_goal_seconds:
                        timed_out = True
                        state = service.cancel()
                        break
                    state = coordinator.step(GOAL)
                    if not state.terminal:
                        time.sleep(1)

                report = morning_report(root)
                results.append(
                    {
                        "case": case.name,
                        "scenario": case.scenario,
                        "repeat": repeat + 1,
                        "complete": state.phase == "delivered"
                        and state.observation.verified
                        and not timed_out,
                        "external_blocker": state.observation.failure == "environment",
                        "candidate_sha256": state.observation.candidate_sha256,
                        "outcome": state.phase,
                        "failure_kind": state.observation.failure,
                        **trial_metrics(root, state, case),
                        "duration_seconds": time.monotonic() - started,
                        "paid_spent_microusd": report["paid_spent_microusd"],
                        "unknown_reserved_microusd": report["paid_reserved_microusd"],
                        "timed_out": timed_out,
                    }
                )
            finally:
                if stack is not None:
                    stack.close()
                service.close()
            (directory / "results.json").write_text(json.dumps(results, indent=2))
    passed = qualification_passed(plan, results)
    result: dict[str, object] = {
        "qualified": passed,
        "live": True,
        "results": results,
        "frozen_threshold": plan.minimum_completion_rate,
        "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
    }
    (directory / "qualification.json").write_text(json.dumps(result, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, default=Path(".loopforge/secrets.env"))
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    plan = QualificationPlan.model_validate_json(args.plan.read_text())
    if not args.live:
        print(
            json.dumps(
                {
                    "qualified": False,
                    "status": "validated-plan-only",
                    "calls": 0,
                    "allocated_cap_microusd": plan.paid_cap_microusd,
                }
            )
        )
        return
    print(json.dumps(qualify(plan, args.directory, args.key_file), indent=2))


if __name__ == "__main__":
    main()
