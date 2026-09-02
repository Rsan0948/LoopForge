from __future__ import annotations

from pathlib import Path

import pytest

from loopforge.domain.routing import ModelTier
from loopforge.entrypoints.profile import LoopProfile, ProfileError, load_profile
from loopforge.workloads.repair import RepairCommandKind


def _make_repo(root: Path, *, with_venv: bool = True) -> Path:
    (root / ".git").mkdir(parents=True)
    if with_venv:
        venv_bin = root / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").touch()
    return root


def _write_profile(tmp_path: Path, body: str) -> Path:
    profile = tmp_path / "profile.toml"
    profile.write_text(body, encoding="utf-8")
    return profile


def _valid_body(repository: Path) -> str:
    return f"""
[task]
id = "demo-loop"
objective = "Fix the failing checks."
repository = "{repository}"

[[checks]]
name = "unit_tests"
kind = "TEST"
argv = ["{{python}}", "-m", "pytest", "-q", "tests/unit"]
timeout_seconds = 300

[[checks]]
name = "ruff"
kind = "LINT"
argv = ["{{python}}", "-m", "ruff", "check", "src"]
timeout_seconds = 120
cpu_seconds = 90

[acceptance]
required = ["unit_tests", "ruff"]
require_change = true
allowed_prefixes = ["src", "tests", "pyproject.toml"]
max_changed_files = 30

[sandbox]
environment = {{ EXAMPLE_ENV = "test" }}

[model]
provider = "scripted"
tier = "economy"

[budget]
max_cost_usd = 5.0
max_iterations = 30
"""


def test_load_valid_local_profile(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    profile = load_profile(_write_profile(tmp_path, _valid_body(repo)))

    assert isinstance(profile, LoopProfile)
    assert profile.repository == repo
    assert profile.container_image is None
    assert profile.environment == {"EXAMPLE_ENV": "test"}
    assert profile.limits.max_memory_bytes == 2 * 1024 * 1024 * 1024
    assert profile.model_provider == "scripted"
    assert profile.model_name == "scripted"
    assert profile.model_tier is ModelTier.ECONOMY
    assert profile.budget.max_cost_usd == 5.0
    assert profile.budget.max_iterations == 30

    task = profile.task
    assert task.task_id == "demo-loop"
    assert [command.name for command in task.commands] == ["unit_tests", "ruff"]
    tests, ruff = task.commands
    assert tests.kind is RepairCommandKind.TEST
    assert tests.argv[0] == str(repo / ".venv" / "bin" / "python")
    assert tests.argv[1:] == ("-m", "pytest", "-q", "tests/unit")
    assert tests.timeout_seconds == 300
    assert tests.cpu_seconds == 300  # defaults to the timeout
    assert ruff.cpu_seconds == 90
    assert task.acceptance.required_commands == ("unit_tests", "ruff")
    assert task.acceptance.patch.require_change is True
    assert task.acceptance.patch.allowed_prefixes == ("src", "tests", "pyproject.toml")
    assert task.acceptance.patch.max_changed_files == 30


def test_load_valid_container_profile(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_venv=False)
    body = (
        _valid_body(repo)
        .replace(
            'argv = ["{python}", "-m", "pytest", "-q", "tests/unit"]',
            'argv = ["/usr/local/bin/python", "-m", "pytest", "-q"]',
        )
        .replace(
            'argv = ["{python}", "-m", "ruff", "check", "src"]',
            'argv = ["/usr/local/bin/ruff", "check", "src"]',
        )
        .replace(
            "[sandbox]",
            '[sandbox]\ncontainer_image = "example:tag"\nmax_memory_bytes = 1073741824',
        )
        .replace('provider = "scripted"', 'provider = "deepseek"\nname = "deepseek-v4-flash"')
        .replace('tier = "economy"', 'tier = "advanced"')
    )
    profile = load_profile(_write_profile(tmp_path, body))

    assert profile.container_image == "example:tag"
    assert profile.limits.max_memory_bytes == 1073741824
    assert profile.model_provider == "deepseek"
    assert profile.model_name == "deepseek-v4-flash"
    assert profile.model_tier is ModelTier.ADVANCED
    assert profile.task.commands[0].argv[0] == "/usr/local/bin/python"


def test_missing_profile_file_is_denied(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "nope.toml")


def test_malformed_toml_is_denied(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path, "[task\nnot toml")
    with pytest.raises(ProfileError, match="invalid TOML"):
        load_profile(profile)


def test_missing_task_table_is_denied(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match=r"\[task\]"):
        load_profile(_write_profile(tmp_path, "[model]\n"))


def test_relative_repository_is_denied(tmp_path: Path) -> None:
    body = _valid_body(Path("relative/repo"))
    with pytest.raises(ProfileError, match="absolute path"):
        load_profile(_write_profile(tmp_path, body))


def test_nonexistent_repository_is_denied(tmp_path: Path) -> None:
    body = _valid_body(tmp_path / "ghost")
    with pytest.raises(ProfileError, match="does not exist"):
        load_profile(_write_profile(tmp_path, body))


def test_repository_without_git_is_denied(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").touch()
    with pytest.raises(ProfileError, match="git worktree"):
        load_profile(_write_profile(tmp_path, _valid_body(repo)))


def test_missing_local_interpreter_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_venv=False)
    with pytest.raises(ProfileError, match="interpreter not found"):
        load_profile(_write_profile(tmp_path, _valid_body(repo)))


def test_empty_checks_are_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).split("[[checks]]")[0]
    with pytest.raises(ProfileError, match="at least one"):
        load_profile(_write_profile(tmp_path, body))


def test_duplicate_check_names_are_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace('name = "ruff"', 'name = "unit_tests"')
    with pytest.raises(ProfileError, match="duplicate check name"):
        load_profile(_write_profile(tmp_path, body))


def test_unknown_check_kind_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace('kind = "TEST"', 'kind = "VIBES"')
    with pytest.raises(ProfileError, match="kind"):
        load_profile(_write_profile(tmp_path, body))


def test_relative_argv_executable_in_local_mode_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace('argv = ["{python}", "-m", "ruff"', 'argv = ["ruff", "check"')
    with pytest.raises(ProfileError, match="absolute path"):
        load_profile(_write_profile(tmp_path, body))


def test_python_token_in_container_mode_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_venv=False)
    body = _valid_body(repo).replace("[sandbox]", '[sandbox]\ncontainer_image = "example:tag"')
    with pytest.raises(ProfileError, match="container mode"):
        load_profile(_write_profile(tmp_path, body))


def test_unknown_required_check_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace(
        'required = ["unit_tests", "ruff"]', 'required = ["unit_tests", "nope"]'
    )
    with pytest.raises(ProfileError, match="unknown checks"):
        load_profile(_write_profile(tmp_path, body))


def test_dotdot_prefix_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace('"src", "tests"', '"src", "../secrets"')
    with pytest.raises(ProfileError, match="allowed_prefixes"):
        load_profile(_write_profile(tmp_path, body))


def test_invalid_environment_key_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace("EXAMPLE_ENV", "9BAD-KEY")
    with pytest.raises(ProfileError, match="variable name"):
        load_profile(_write_profile(tmp_path, body))


def test_non_string_environment_value_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace('EXAMPLE_ENV = "test"', "EXAMPLE_ENV = 42")
    with pytest.raises(ProfileError, match="must be a string"):
        load_profile(_write_profile(tmp_path, body))


def test_invalid_container_image_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_venv=False)
    body = _valid_body(repo).replace("[sandbox]", '[sandbox]\ncontainer_image = "bad image"')
    with pytest.raises(ProfileError, match="image reference"):
        load_profile(_write_profile(tmp_path, body))


def test_unknown_model_provider_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace('provider = "scripted"', 'provider = "skynet"')
    with pytest.raises(ProfileError, match="provider"):
        load_profile(_write_profile(tmp_path, body))


def test_live_provider_without_model_name_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace('provider = "scripted"', 'provider = "deepseek"')
    with pytest.raises(ProfileError, match=r"model\.name"):
        load_profile(_write_profile(tmp_path, body))


def test_unknown_model_tier_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace('tier = "economy"', 'tier = "deluxe"')
    with pytest.raises(ProfileError, match="tier"):
        load_profile(_write_profile(tmp_path, body))


def test_non_positive_budget_cost_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace("max_cost_usd = 5.0", "max_cost_usd = -1.0")
    with pytest.raises(ProfileError, match="max_cost_usd"):
        load_profile(_write_profile(tmp_path, body))


def test_missing_budget_cost_reports_required_not_invalid(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace("max_cost_usd = 5.0\n", "")
    with pytest.raises(ProfileError, match="max_cost_usd is required"):
        load_profile(_write_profile(tmp_path, body))


def test_missing_budget_iterations_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace("max_iterations = 30\n", "")
    with pytest.raises(ProfileError, match="max_iterations"):
        load_profile(_write_profile(tmp_path, body))


def test_non_positive_timeout_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace("timeout_seconds = 300", "timeout_seconds = 0")
    with pytest.raises(ProfileError, match="timeout_seconds"):
        load_profile(_write_profile(tmp_path, body))


def test_optional_budget_limits_are_loaded(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo).replace(
        "max_iterations = 30",
        "max_iterations = 30\nmax_total_tokens = 100000\nmax_elapsed_seconds = 600",
    )
    profile = load_profile(_write_profile(tmp_path, body))
    assert profile.budget.max_total_tokens == 100000
    assert profile.budget.max_elapsed_seconds == 600


def test_approval_required_for_is_loaded(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo) + '\n[approval]\nrequired_for = ["write_file", "edit_file"]\n'
    profile = load_profile(_write_profile(tmp_path, body))
    assert profile.approval_required_for == frozenset({"write_file", "edit_file"})


def test_approval_required_for_defaults_to_empty(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    profile = load_profile(_write_profile(tmp_path, _valid_body(repo)))
    assert profile.approval_required_for == frozenset()


def test_approval_required_for_non_list_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo) + '\n[approval]\nrequired_for = "write_file"\n'
    with pytest.raises(ProfileError, match=r"approval\.required_for"):
        load_profile(_write_profile(tmp_path, body))


def test_approval_required_for_bad_entry_is_denied(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    body = _valid_body(repo) + '\n[approval]\nrequired_for = ["Write File!"]\n'
    with pytest.raises(ProfileError, match=r"approval\.required_for"):
        load_profile(_write_profile(tmp_path, body))
