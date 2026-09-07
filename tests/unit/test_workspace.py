"""Unit tests for the workspace, verification, and artifact domain vocabulary."""

from __future__ import annotations

import pytest

from loopforge.domain.artifacts import ArtifactKind
from loopforge.domain.verification import CheckOutcome, compose_check_outcomes
from loopforge.domain.workspace import (
    AcceptanceCriteria,
    FixtureFile,
    FixtureSpec,
    PatchConstraints,
    WorkspaceStatus,
)


def _fixture_file(path: str = "adder.py", content: str = "x = 1\n") -> FixtureFile:
    return FixtureFile(path=path, content=content)


def _fixture(**overrides: object) -> FixtureSpec:
    kwargs: dict[str, object] = {
        "fixture_id": "fixture-1",
        "files": (_fixture_file(),),
    }
    kwargs.update(overrides)
    return FixtureSpec(**kwargs)  # pyright: ignore[reportArgumentType]


def test_fixture_file_rejects_empty_absolute_and_parent_paths() -> None:
    with pytest.raises(ValueError, match="fixture file path cannot be empty"):
        _fixture_file(path="  ")
    with pytest.raises(ValueError, match="must be a relative path"):
        _fixture_file(path="/etc/passwd")
    with pytest.raises(ValueError, match="must be a relative path"):
        _fixture_file(path="../escape.py")
    with pytest.raises(ValueError, match="must be a relative path"):
        _fixture_file(path="a//b.py")
    with pytest.raises(ValueError, match="must be a relative path"):
        _fixture_file(path="./a.py")


def test_fixture_spec_rejects_invalid_id_and_empty_files() -> None:
    with pytest.raises(ValueError, match="fixture_id cannot be empty"):
        _fixture(fixture_id=" ")
    with pytest.raises(ValueError, match="plain name without path separators"):
        _fixture(fixture_id="a/b")
    with pytest.raises(ValueError, match="plain name without path separators"):
        _fixture(fixture_id="..")
    with pytest.raises(ValueError, match="at least one file"):
        _fixture(files=())


def test_fixture_spec_rejects_duplicate_and_orphan_paths() -> None:
    with pytest.raises(ValueError, match="fixture file paths must be unique"):
        _fixture(files=(_fixture_file(), _fixture_file()))
    with pytest.raises(ValueError, match="solution paths must be unique"):
        _fixture(solution=(_fixture_file(), _fixture_file()))
    with pytest.raises(ValueError, match="is not part of the fixture"):
        _fixture(solution=(_fixture_file(path="other.py"),))


def test_workspace_status_files_union_sorted() -> None:
    status = WorkspaceStatus(changed=("b.py", "a.py"), untracked=("a.py", "c.py"))
    assert status.files == ("a.py", "b.py", "c.py")
    assert not status.clean
    assert WorkspaceStatus().clean


def test_patch_constraints_validation() -> None:
    with pytest.raises(ValueError, match="allowed path prefix must be a relative path"):
        PatchConstraints(allowed_prefixes=("../secret",))
    with pytest.raises(ValueError, match="max_changed_files must be a positive int"):
        PatchConstraints(max_changed_files=0)
    with pytest.raises(ValueError, match="max_changed_files must be a positive int"):
        PatchConstraints(max_changed_files=True)
    with pytest.raises(ValueError, match="max_changed_files must be a positive int"):
        PatchConstraints(max_changed_files=float("nan"))  # type: ignore[arg-type]


def test_acceptance_criteria_validation() -> None:
    with pytest.raises(ValueError, match="required command names cannot be empty"):
        AcceptanceCriteria(required_commands=(" ",))
    with pytest.raises(ValueError, match="required command names must be unique"):
        AcceptanceCriteria(required_commands=("run_tests", "run_tests"))


def test_check_outcome_requires_name() -> None:
    with pytest.raises(ValueError, match="check outcome name cannot be empty"):
        CheckOutcome(name=" ", passed=True, detail="ok")


def test_compose_check_outcomes_is_conjunctive_and_deterministic() -> None:
    outcomes = (
        CheckOutcome(name="command:run_tests", passed=True, detail="exit_code=0"),
        CheckOutcome(name="patch_constraints", passed=False, detail="no workspace changes"),
    )

    composite = compose_check_outcomes(outcomes)

    assert composite.passed is False
    assert composite.score == 0.5
    assert composite.summary == (
        "command:run_tests: passed (exit_code=0); patch_constraints: failed (no workspace changes)"
    )
    assert compose_check_outcomes(outcomes) == composite


def test_compose_check_outcomes_flags_checks_that_never_executed() -> None:
    outcomes = (
        CheckOutcome(
            name="command:tests",
            passed=False,
            detail="sandbox error: launcher failed",
            executed=False,
        ),
        CheckOutcome(name="patch_constraints", passed=True, detail="ok"),
    )
    composite = compose_check_outcomes(outcomes)
    assert composite.passed is False
    assert composite.inconclusive is True
    # Fail-closed scoring is unchanged: an unexecuted check still contributes 0.
    assert composite.score == 0.5
    all_executed = compose_check_outcomes(
        (CheckOutcome(name="only", passed=False, detail="exit_code=1"),)
    )
    assert all_executed.inconclusive is False


def test_compose_check_outcomes_all_pass_scores_one() -> None:
    composite = compose_check_outcomes((CheckOutcome(name="only", passed=True, detail="ok"),))
    assert composite.passed is True
    assert composite.score == 1.0


def test_compose_check_outcomes_requires_at_least_one_check() -> None:
    with pytest.raises(ValueError, match="verification requires at least one check"):
        compose_check_outcomes(())


def test_artifact_kind_vocabulary_is_closed() -> None:
    assert [kind.value for kind in ArtifactKind] == ["workspace_snapshot"]
