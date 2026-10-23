"""Regression pins for multi-role authority, review, clarification and runner control."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from loopforge.adapters.gemini_review import GeminiReviewModel
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.goal_journal import SQLiteGoalJournal, decode_goal_event, encode_goal_event
from loopforge.adapters.paid_budget import SQLitePaidBudget
from loopforge.application.paid_supervisor import BudgetedSupervisor
from loopforge.domain.context import ContextItem, ContextSource, ModelContext
from loopforge.domain.goal_review import (
    ClarificationPolicy,
    ClarificationRequest,
    ExperimentRecorded,
    QuestionOpened,
)
from loopforge.domain.goals import (
    GoalAction,
    GoalCreated,
    GoalPolicy,
    GoalState,
    SupervisorDecision,
    reduce_goal,
)
from loopforge.domain.investigation import TaskAssignment
from loopforge.domain.security import TrustClass
from loopforge.domain.types import ContextItemId, RunId
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints.campaign_coding import writable
from loopforge.entrypoints.campaign_models import CodingCheck, CodingTaskConfig
from loopforge.entrypoints.goal_runner import GoalExecutionService, record_runner, runner_record
from loopforge.entrypoints.supervisor import GOAL
from loopforge.ports.model import ModelToolSpec, ModelTurnError
from loopforge.ports.supervisor import SupervisorFailure, SupervisorRequest, SupervisorResponse


def test_a10_project_maintenance_is_explicit_while_trusted_and_secret_paths_stay_denied(
    tmp_path: Path,
) -> None:
    workspace = GitWorkspaceManager(tmp_path).materialize(
        FixtureSpec(
            fixture_id="approved-maintenance",
            files=(
                FixtureFile(path="tests/test_api.py", content="assert True\n"),
                FixtureFile(path="requirements.txt", content="package==1\n"),
                FixtureFile(path="oracle.py", content="assert True\n"),
            ),
        )
    )
    task = CodingTaskConfig(
        objective="Update approved project dependency and regression test",
        kind="feature",
        branch="feature/maintenance",
        allowed_paths=["tests", "requirements.txt", "oracle.py"],
        checks=[CodingCheck(name="check_project", argv=["/usr/bin/true"])],
        image="sha256:" + "a" * 64,
        evidence="Trusted intake",
    )
    assert not writable("tests/test_api.py", task, workspace)
    allowed = task.model_copy(
        update={
            "editable_project_paths": ["tests/test_api.py", "requirements.txt"],
            "immutable_paths": ["oracle.py"],
        }
    )
    assert writable("tests/test_api.py", allowed, workspace)
    assert writable("requirements.txt", allowed, workspace)
    assert not writable("oracle.py", allowed, workspace)
    assert not writable(".env", allowed, workspace)
    with pytest.raises(ValueError, match="trusted or secret"):
        CodingTaskConfig.model_validate(
            task.model_dump()
            | {"editable_project_paths": ["oracle.py"], "immutable_paths": ["oracle.py"]}
        )
    with pytest.raises(ValueError, match="trusted or secret"):
        CodingTaskConfig.model_validate(
            task.model_dump() | {"allowed_paths": [".env"], "editable_project_paths": [".env"]}
        )


def test_a12_all_roles_share_atomic_cap_and_unknown_reservations(tmp_path: Path) -> None:
    path = tmp_path / "paid.sqlite"
    budget = SQLitePaidBudget(path, 100)

    def reserve(role: str) -> bool:
        try:
            SQLitePaidBudget(path, 100).reserve(role, role, 60)
        except ValueError:
            return False
        else:
            return True

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(reserve, ["supervisor", "review", "repair"]))
    assert sum(outcomes) == 1
    account = budget.account()
    assert account.spent == 0
    assert sum(p[1] for p in account.pending) == 60
    key = account.pending[0][0]
    with pytest.raises(ValueError, match="reservation denied"):
        budget.reserve(key, "repair", 1)
    budget.settle(key, 25)
    budget.reserve("next", "repair", 75)
    assert budget.account().spent == 25
    with pytest.raises(ValueError, match="reservation denied"):
        budget.reserve("excess", "review", 1)
    with pytest.raises(ValueError, match="immutable"):
        SQLitePaidBudget(path, 101)


def test_a12_parent_reserves_before_provider_and_preserves_delegate_trace(tmp_path: Path) -> None:
    class Provider:
        calls = 0

        def prepare(self, state: GoalState) -> SupervisorRequest:
            del state
            return SupervisorRequest("{}", 60, json.dumps({"input_bound": 10}))

        def decide(self, request: SupervisorRequest) -> SupervisorResponse:
            self.calls += 1
            assert json.loads(request.trace)["input_bound"] == 10
            return SupervisorResponse(
                SupervisorDecision(GoalAction.BLOCK, "Observed blocker", "Retain evidence"),
                20,
                "fixture",
            )

    budget = SQLitePaidBudget(tmp_path / "paid.sqlite", 100)
    provider = Provider()
    wrapped = BudgetedSupervisor(provider, budget)
    first = wrapped.prepare(GoalState(sequence=1))
    wrapped.decide(first)
    second = wrapped.prepare(GoalState(sequence=2))
    wrapped.decide(second)
    with pytest.raises(SupervisorFailure):
        wrapped.decide(first)
    assert provider.calls == 2
    assert budget.account().spent == 40


@pytest.mark.parametrize("classification", ["optional", "permission"])
def test_a09_midtask_question_category_and_fallback_cannot_grant_authority(
    classification: str,
) -> None:
    template = ClarificationPolicy(
        "design",
        "Approved design question",
        classification,
        10,
        "Use the current layout" if classification == "optional" else "",
    )
    policy = GoalPolicy(
        "Implement a bounded feature",
        "a" * 64,
        1000,
        1000,
        dynamic_clarifications=(template,),
        checkpoint_revision="b" * 40,
    )
    assignment = TaskAssignment("Implement an approved feature", ("src/a.py",), ("e_source",))
    request = ClarificationRequest("layout", "design", "Should we preserve the existing layout?")
    state = GoalState(
        policy=policy,
        phase="action",
        decision=SupervisorDecision(
            GoalAction.ASK, "Resolve ambiguity", "Continue the feature", question=request
        ),
        assignment=assignment,
        last_verified_candidate="c" * 64,
    )
    opened = QuestionOpened("layout", 110, request.question, "design")
    assert decode_goal_event(encode_goal_event(opened)) == opened
    state = reduce_goal(state, opened)
    assert state.dynamic_questions[0].classification == classification
    experiment = ExperimentRecorded("layout", "experiment/" + "d" * 20, "c" * 64, 111)
    if classification == "permission":
        with pytest.raises(ValueError, match="preauthorized"):
            reduce_goal(state, experiment)
    else:
        state = reduce_goal(state, experiment)
        assert state.experiments[0].assumption == "Use the current layout"
        assert state.policy == policy
    bad = replace(opened, topic="another", category="unapproved")
    with pytest.raises(ValueError, match="frozen category"):
        reduce_goal(state, bad)


def test_a14_only_explicitly_armed_goals_run_and_restart_recovers_intent(tmp_path: Path) -> None:
    directory = tmp_path / "goal"
    directory.mkdir()
    journal = SQLiteGoalJournal(directory / "goals.sqlite")
    journal.append(GOAL, 0, GoalCreated(GoalPolicy("Run an approved task", "a" * 64, 1000, 1000)))
    calls: list[Path] = []
    done = threading.Event()

    def drive(path: Path, stop: threading.Event) -> None:
        calls.append(path)
        done.set()

    service = GoalExecutionService(tmp_path, check=lambda _: None, drive=drive)
    service.start()
    assert not done.wait(0.05)
    service.close()
    record_runner(directory, armed=True, status="offline")
    restarted = GoalExecutionService(tmp_path, check=lambda _: None, drive=drive)
    restarted.start()
    assert done.wait(2)
    restarted.close()
    assert calls == [directory]
    assert runner_record(directory)["status"] == "stopped"
    assert journal.load(GOAL).phase == "ready"  # Liveness never rewrites authoritative state.


def test_a14_missing_dependencies_do_not_arm_an_unattended_goal(tmp_path: Path) -> None:
    directory = tmp_path / "goal"
    directory.mkdir()
    SQLiteGoalJournal(directory / "goals.sqlite")

    def deny(_: Path) -> None:
        msg = "Container unavailable"
        raise ValueError(msg)

    runner = GoalExecutionService(tmp_path, check=deny)
    with pytest.raises(ValueError, match="Container unavailable"):
        runner.arm(directory)
    assert runner_record(directory) == {"status": "offline", "armed": False}


def test_a11_frontier_review_is_paid_identity_visible_and_cannot_invent_tools(
    tmp_path: Path,
) -> None:

    calls: list[dict[str, object]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {"tool_name": "invent_verified", "arguments": {}}
                                    )
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 20,
                    "totalTokenCount": 120,
                },
            },
        )

    budget = SQLitePaidBudget(tmp_path / "paid.sqlite", 500000)
    now = datetime.now(UTC)
    context = ModelContext(
        run_id=RunId("run_review"),
        items=(
            ContextItem(
                item_id=ContextItemId("candidate"),
                content="Exact candidate identity: " + "b" * 64,
                trust=TrustClass.DETERMINISTIC_OBSERVATION,
                source=ContextSource(
                    origin=TrustClass.DETERMINISTIC_OBSERVATION, reference="current-candidate"
                ),
                created_at=now,
            ),
        ),
        assembled_at=now,
    )
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        reviewer = GeminiReviewModel(
            "dummy-review-key",
            budget,
            (
                ModelToolSpec(
                    name="read_file",
                    description="Read actual source",
                    parameters={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                ),
            ),
            tmp_path / "traces",
            client=client,
        )
        with pytest.raises(ModelTurnError):
            reviewer.propose_action(context)
    assert "b" * 64 in json.dumps(calls[0])
    assert budget.account().spent == 400
    assert not budget.account().pending
