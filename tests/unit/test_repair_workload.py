"""Unit tests for the repair workload: tasks, fixtures, artifacts, and context trust."""

from __future__ import annotations

import math
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopforge.adapters.context import (
    BasicContextBuilder,
    BudgetedContextBuilder,
    CharsPerTokenCounter,
)
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.scripted import FixedClock
from loopforge.domain.artifacts import ArtifactKind
from loopforge.domain.context import ContextAuthorityError, promote
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.prompts import default_controller_template
from loopforge.domain.security import TrustClass
from loopforge.domain.state import RunState
from loopforge.domain.types import RunId
from loopforge.domain.workspace import (
    AcceptanceCriteria,
    FixtureFile,
    FixtureSpec,
    PatchConstraints,
)
from loopforge.ports.artifacts import RunArtifact
from loopforge.ports.workspace import WorkspaceError
from loopforge.workloads.fixtures import adder_fixture, adder_repair_task
from loopforge.workloads.repair import (
    UNTRUSTED_REPAIR_REQUIREMENTS,
    RepairCommand,
    RepairCommandKind,
    RepairContextBuilder,
    RepairTask,
    WorkspaceArtifactCollector,
    scripted_repair_actions,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
RUN_ID = RunId("run-workload")

_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git executable unavailable; artifact collector tests require the Git CLI",
)


def test_untrusted_repair_requirements_are_code_owned_and_fixed() -> None:
    requirements = UNTRUSTED_REPAIR_REQUIREMENTS
    assert requirements.process_filesystem_isolated
    assert requirements.network_isolated
    assert not requirements.kernel_isolated
    assert not requirements.file_api_confined
    assert not requirements.resource_limits


def test_repair_command_validation() -> None:
    with pytest.raises(ValueError, match="repair command name cannot be empty"):
        RepairCommand(kind=RepairCommandKind.TEST, name=" ", argv=("/bin/true",))
    with pytest.raises(ValueError, match="absolute executable path"):
        RepairCommand(kind=RepairCommandKind.TEST, name="run_tests", argv=())
    with pytest.raises(ValueError, match="absolute executable path"):
        RepairCommand(kind=RepairCommandKind.TEST, name="run_tests", argv=("python", "-V"))
    with pytest.raises(ValueError, match="timeout must be positive and finite"):
        RepairCommand(
            kind=RepairCommandKind.TEST, name="run_tests", argv=("/bin/true",), timeout_seconds=0
        )
    with pytest.raises(ValueError, match="timeout must be positive and finite"):
        RepairCommand(
            kind=RepairCommandKind.TEST,
            name="run_tests",
            argv=("/bin/true",),
            timeout_seconds=math.inf,
        )
    with pytest.raises(ValueError, match="cpu seconds must be positive and finite"):
        RepairCommand(
            kind=RepairCommandKind.TEST, name="run_tests", argv=("/bin/true",), cpu_seconds=0
        )


def test_repair_task_validation() -> None:
    task = adder_repair_task(executable="/usr/bin/python3")
    with pytest.raises(ValueError, match="repair task_id cannot be empty"):
        RepairTask(
            task_id=" ",
            objective=task.objective,
            fixture=task.fixture,
            commands=task.commands,
            acceptance=task.acceptance,
        )
    with pytest.raises(ValueError, match="repair command names must be unique"):
        RepairTask(
            task_id="t",
            objective=task.objective,
            fixture=task.fixture,
            commands=(task.commands[0], task.commands[0]),
            acceptance=task.acceptance,
        )
    with pytest.raises(ValueError, match="undefined commands: missing"):
        RepairTask(
            task_id="t",
            objective=task.objective,
            fixture=task.fixture,
            commands=task.commands,
            acceptance=AcceptanceCriteria(required_commands=("missing",)),
        )
    with pytest.raises(ValueError, match="repair task objective cannot be empty"):
        RepairTask(
            task_id="t",
            objective=" ",
            fixture=task.fixture,
            commands=task.commands,
            acceptance=task.acceptance,
        )
    # Vacuous success is rejected: an acceptance contract demanding nothing
    # would be satisfied by nothing.
    with pytest.raises(ValueError, match="must require at least one command"):
        RepairTask(
            task_id="t",
            objective=task.objective,
            fixture=task.fixture,
            commands=task.commands,
            acceptance=AcceptanceCriteria(),
        )


def test_adder_fixture_is_well_formed() -> None:
    fixture = adder_fixture()
    paths = [item.path for item in fixture.files]
    assert "adder.py" in paths
    assert "tests/test_adder.py" in paths
    assert [item.path for item in fixture.solution] == ["adder.py"]
    buggy = next(item.content for item in fixture.files if item.path == "adder.py")
    fixed = fixture.solution[0].content
    assert "return left - right" in buggy
    assert "return left + right" in fixed


def test_adder_repair_task_uses_executable_and_references_defined_commands() -> None:
    task = adder_repair_task(executable="/custom/python")
    assert all(command.argv[0] == "/custom/python" for command in task.commands)
    assert task.acceptance.required_commands == ("run_tests",)
    assert {command.kind for command in task.commands} == {
        RepairCommandKind.TEST,
        RepairCommandKind.BUILD,
    }


def test_scripted_repair_actions_read_then_write_solution() -> None:
    task = adder_repair_task(executable="/usr/bin/python3")
    actions = scripted_repair_actions(task)
    assert [action.tool_name for action in actions] == ["read_file", "write_file"]
    assert actions[0].arguments == {"path": "adder.py"}
    assert actions[1].arguments["path"] == "adder.py"
    assert actions[1].arguments["content"] == task.fixture.solution[0].content


@_REQUIRES_GIT
def test_artifact_collector_captures_exact_patch_and_inventory(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path / "ws").materialize(adder_fixture())
    collector = WorkspaceArtifactCollector(workspace)

    (workspace.root / "adder.py").write_text(adder_fixture().solution[0].content, encoding="utf-8")
    artifacts = collector.collect(RunState(run_id=RUN_ID))

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.kind is ArtifactKind.WORKSPACE_SNAPSHOT
    assert artifact.label == "workspace:adder-regression"
    assert f"base_revision={workspace.base_revision}" in artifact.content
    assert "changed_files=adder.py" in artifact.content
    assert "-    return left - right" in artifact.content
    assert "+    return left + right" in artifact.content


@_REQUIRES_GIT
def test_artifact_collector_fails_loudly_over_budget(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path / "ws").materialize(adder_fixture())
    collector = WorkspaceArtifactCollector(workspace, max_artifact_bytes=8)

    with pytest.raises(WorkspaceError, match="artifact byte budget"):
        collector.collect(RunState(run_id=RUN_ID))

    with pytest.raises(ValueError, match="max_artifact_bytes must be positive"):
        WorkspaceArtifactCollector(workspace, max_artifact_bytes=0)


def test_run_artifact_requires_label() -> None:
    with pytest.raises(ValueError, match="artifact label cannot be empty"):
        RunArtifact(kind=ArtifactKind.WORKSPACE_SNAPSHOT, label=" ", content="x")


def _state_with_observation() -> RunState:
    return RunState(
        run_id=RUN_ID,
        objective="repair the fixture",
        plan="inspect then patch",
        last_observation="file content from the repository",
        last_verification="command:run_tests: failed (exit_code=1)",
        last_verification_passed=False,
    )


def test_repair_context_builder_demotes_observations_only() -> None:
    builder = RepairContextBuilder(BasicContextBuilder(FixedClock(NOW)))

    context = builder.build_context(_state_with_observation())

    by_key = {item.item_id.split(":")[-1]: item for item in context.items}
    assert by_key["observation"].trust is TrustClass.UNTRUSTED_CONTENT
    assert by_key["observation"].source.origin is TrustClass.UNTRUSTED_CONTENT
    assert "untrusted repository output" in by_key["observation"].source.detail
    # Runtime-owned items keep their trust classes.
    assert by_key["objective"].trust is TrustClass.AUTHORIZED_HUMAN
    assert by_key["plan"].trust is TrustClass.RUNTIME_POLICY
    assert by_key["verification"].trust is TrustClass.DETERMINISTIC_OBSERVATION


def test_demoted_observation_can_never_elevate_to_policy() -> None:
    builder = RepairContextBuilder(BasicContextBuilder(FixedClock(NOW)))
    context = builder.build_context(_state_with_observation())
    observation = next(item for item in context.items if item.item_id.endswith(":observation"))

    with pytest.raises(ContextAuthorityError, match="can never be promoted"):
        promote(observation, to=TrustClass.RUNTIME_POLICY, basis="verifier:run_tests")


def test_repair_context_builder_passes_through_accounting() -> None:
    budgeted = BudgetedContextBuilder(
        FixedClock(NOW),
        CharsPerTokenCounter(),
        template=default_controller_template(),
        token_budget=ContextTokenBudget(max_tokens=4096, reserve_tokens=256),
    )
    builder = RepairContextBuilder(budgeted)

    assert builder.last_accounting is None
    builder.build_context(_state_with_observation())
    assert builder.last_accounting is not None
    assert builder.last_accounting is budgeted.last_accounting


def test_fixture_without_solution_yields_read_only_script() -> None:
    task = RepairTask(
        task_id="t",
        objective="inspect only",
        fixture=FixtureSpec(fixture_id="bare", files=(FixtureFile(path="a.py", content=""),)),
        commands=(
            RepairCommand(kind=RepairCommandKind.TEST, name="run_tests", argv=("/bin/true",)),
        ),
        acceptance=AcceptanceCriteria(
            required_commands=("run_tests",), patch=PatchConstraints(require_change=False)
        ),
    )
    actions = scripted_repair_actions(task)
    assert [action.tool_name for action in actions] == ["read_file"]
