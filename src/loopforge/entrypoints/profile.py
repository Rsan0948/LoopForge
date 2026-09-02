"""Operator-owned local loop profiles (``.loopforge/*.toml``, gitignored).

A profile generalizes the hardcoded ``civicml-loop`` wiring: it describes one
adopted-checkout repair loop — target repository, sandbox checks, acceptance
contract, sandbox environment, model selection, and budget — so the same
bounded runtime can target any local repository, including LoopForge itself.

Authority model (AGENTS.md rules 14 and 16): a profile is *operator*
authority, exactly like the code-owned wiring in ``cli.py``. Profiles must
therefore live OUTSIDE the target repository (LoopForge's own gitignored
``.loopforge/`` directory is the intended home); target-repository content
may never supply or widen a profile. Every field is validated here at load
time — an invalid profile is unconstructable, mirroring the domain's own
fail-closed constructors.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from loopforge.adapters.local_sandbox import SandboxLimits
from loopforge.domain.routing import ModelTier
from loopforge.domain.types import BudgetLimit
from loopforge.domain.workspace import (
    AcceptanceCriteria,
    FixtureFile,
    FixtureSpec,
    PatchConstraints,
)
from loopforge.workloads.repair import RepairCommand, RepairCommandKind, RepairTask

_DEFAULT_MEMORY_BYTES: Final = 2 * 1024 * 1024 * 1024
_DEFAULT_LOCAL_PYTHON: Final = ".venv/bin/python"
_LOCAL_PYTHON_TOKEN: Final = "{python}"
_ENV_KEY_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CHECK_KINDS: Final = {kind.name: kind for kind in RepairCommandKind}
_MODEL_PROVIDERS: Final = ("scripted", "ollama", "deepseek")
_TOOL_NAME_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]*")
_MODEL_TIERS: Final = {
    "economy": ModelTier.ECONOMY,
    "standard": ModelTier.STANDARD,
    "advanced": ModelTier.ADVANCED,
}


class ProfileError(ValueError):
    """Raised when a loop profile is missing, malformed, or out of contract."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LoopProfile:
    """One validated operator-owned repair-loop configuration."""

    task: RepairTask
    repository: Path
    container_image: str | None
    environment: dict[str, str]
    limits: SandboxLimits
    model_provider: str
    model_name: str
    model_tier: ModelTier
    budget: BudgetLimit
    no_progress_limit: int | None
    """Operator-tuned stall threshold (PACS-014b); None = ControlPolicy default."""
    approval_required_for: frozenset[str]
    """Tools the operator gates behind durable approval (PACS-014); empty = none."""


def load_profile(path: str | Path) -> LoopProfile:  # noqa: PLR0915 - sequential validation pipeline
    """Load and validate a loop profile TOML file.

    Raises :class:`ProfileError` on any contract violation — callers never
    see a partially valid configuration.
    """
    profile_path = Path(path)
    if not profile_path.is_file():
        msg = f"profile file not found: {profile_path}"
        raise ProfileError(msg)
    try:
        with profile_path.open("rb") as handle:
            raw: dict[str, Any] = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        msg_2 = f"invalid TOML in {profile_path}: {exc}"
        raise ProfileError(msg_2) from exc

    task_table = _require_table(raw, "task")
    task_id = _require_str(task_table, "id", section="task")
    objective = _require_str(task_table, "objective", section="task")
    repository = _require_repository(task_table)

    sandbox_table = _optional_table(raw, "sandbox")
    container_image = _optional_str(sandbox_table, "container_image", section="sandbox")
    if container_image is not None:
        _validate_image(container_image)
    environment = _require_environment(sandbox_table)
    limits = SandboxLimits(
        max_memory_bytes=_optional_positive_int(
            sandbox_table,
            "max_memory_bytes",
            section="sandbox",
            default=_DEFAULT_MEMORY_BYTES,
        )
    )
    local_python = _optional_str(sandbox_table, "local_python", section="sandbox")
    local_python = local_python if local_python is not None else _DEFAULT_LOCAL_PYTHON
    _validate_relative(local_python, field="sandbox.local_python")
    interpreter = repository / local_python
    if container_image is None and not interpreter.is_file():
        msg_3 = (
            f"sandbox.local_python interpreter not found at {interpreter} "
            "(set container_image or local_python)"
        )
        raise ProfileError(msg_3)

    checks = _require_checks(raw, container=container_image is not None, interpreter=interpreter)
    acceptance = _require_acceptance(raw, checks)

    model_table = _require_table(raw, "model")
    provider = _require_str(model_table, "provider", section="model").lower()
    if provider not in _MODEL_PROVIDERS:
        msg_4 = f"model.provider must be one of {_MODEL_PROVIDERS}, got {provider!r}"
        raise ProfileError(msg_4)
    model_name = _optional_str(model_table, "name", section="model")
    if provider == "scripted":
        model_name = model_name if model_name is not None else "scripted"
    elif model_name is None:
        msg_5 = f"model.name is required for provider {provider!r}"
        raise ProfileError(msg_5)
    tier_name = _require_str(model_table, "tier", section="model").lower()
    tier = _MODEL_TIERS.get(tier_name)
    if tier is None:
        msg_6 = f"model.tier must be one of {tuple(_MODEL_TIERS)}, got {tier_name!r}"
        raise ProfileError(msg_6)

    budget, no_progress_limit = _require_budget(raw)
    approval_required_for = _require_approval(raw)

    try:
        task = RepairTask(
            task_id=task_id,
            objective=objective,
            fixture=FixtureSpec(
                fixture_id=f"adopted-{task_id}",
                files=(FixtureFile(path="pyproject.toml", content="adopted checkout"),),
            ),
            commands=checks,
            acceptance=acceptance,
        )
    except ValueError as exc:
        msg_7 = f"profile is out of contract: {exc}"
        raise ProfileError(msg_7) from exc
    return LoopProfile(
        task=task,
        repository=repository,
        container_image=container_image,
        environment=environment,
        limits=limits,
        model_provider=provider,
        model_name=model_name,
        model_tier=tier,
        budget=budget,
        no_progress_limit=no_progress_limit,
        approval_required_for=approval_required_for,
    )


def _require_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value: object = raw.get(key)
    if not isinstance(value, dict):
        msg = f"missing required [{key}] table"
        raise ProfileError(msg)
    return cast("dict[str, Any]", value)


def _optional_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value: object = raw.get(key, {})
    if not isinstance(value, dict):
        msg = f"[{key}] must be a table"
        raise ProfileError(msg)
    return cast("dict[str, Any]", value)


def _require_str(table: dict[str, Any], key: str, *, section: str) -> str:
    value: object = table.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"{section}.{key} must be a non-empty string"
        raise ProfileError(msg)
    return value


def _optional_str(table: dict[str, Any], key: str, *, section: str) -> str | None:
    value: object = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        msg = f"{section}.{key} must be a non-empty string when set"
        raise ProfileError(msg)
    return value


def _optional_bool(table: dict[str, Any], key: str, *, section: str, default: bool) -> bool:
    value: object = table.get(key, default)
    if not isinstance(value, bool):
        msg = f"{section}.{key} must be a boolean"
        raise ProfileError(msg)
    return value


def _optional_positive_int(table: dict[str, Any], key: str, *, section: str, default: int) -> int:
    value: object = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{section}.{key} must be a positive integer"
        raise ProfileError(msg)
    return value


def _require_positive_number(table: dict[str, Any], key: str, *, section: str) -> float:
    if key not in table:
        msg = f"{section}.{key} is required: set a positive finite number"
        raise ProfileError(msg)
    value: object = table.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        msg = f"{section}.{key} must be a positive finite number"
        raise ProfileError(msg)
    return float(value)


def _require_str_list(table: dict[str, Any], key: str, *, section: str) -> list[str]:
    raw_value: object = table.get(key)
    if not isinstance(raw_value, list) or not raw_value:
        msg = f"{section}.{key} must be a non-empty list of non-empty strings"
        raise ProfileError(msg)
    value = cast("list[Any]", raw_value)
    if any(not isinstance(item, str) or not item.strip() for item in value):
        msg = f"{section}.{key} must be a non-empty list of non-empty strings"
        raise ProfileError(msg)
    return [cast("str", item) for item in value]


def _validate_relative(value: str, *, field: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        msg = f"{field} must be a relative path without '..', got {value!r}"
        raise ProfileError(msg)


def _validate_image(image: str) -> None:
    if image != image.strip() or any(char.isspace() for char in image) or image.startswith("-"):
        msg = f"sandbox.container_image is not a valid image reference: {image!r}"
        raise ProfileError(msg)


def _require_repository(task_table: dict[str, Any]) -> Path:
    raw = _require_str(task_table, "repository", section="task")
    repository = Path(raw)
    if not repository.is_absolute():
        msg = f"task.repository must be an absolute path, got {raw!r}"
        raise ProfileError(msg)
    if not repository.is_dir():
        msg_2 = f"task.repository does not exist: {repository}"
        raise ProfileError(msg_2)
    if not (repository / ".git").exists():
        msg_3 = f"task.repository is not a git worktree (no .git): {repository}"
        raise ProfileError(msg_3)
    return repository


def _require_environment(sandbox_table: dict[str, Any]) -> dict[str, str]:
    value: object = sandbox_table.get("environment", {})
    if not isinstance(value, dict):
        msg = 'sandbox.environment must be a table of KEY = "value" pairs'
        raise ProfileError(msg)
    environment: dict[str, str] = {}
    for key, item in cast("dict[str, Any]", value).items():
        if _ENV_KEY_PATTERN.fullmatch(key) is None:
            msg_2 = f"invalid sandbox.environment variable name: {key!r}"
            raise ProfileError(msg_2)
        if not isinstance(item, str):
            msg_3 = f"sandbox.environment.{key} must be a string"
            raise ProfileError(msg_3)
        environment[key] = item
    return environment


def _require_checks(
    raw: dict[str, Any], *, container: bool, interpreter: Path
) -> tuple[RepairCommand, ...]:
    value: object = raw.get("checks")
    if not isinstance(value, list) or not value:
        msg = "profile must define at least one [[checks]] entry"
        raise ProfileError(msg)
    commands: list[RepairCommand] = []
    names: set[str] = set()
    for index, raw_entry in enumerate(cast("list[Any]", value)):
        if not isinstance(raw_entry, dict):
            msg_2 = f"[[checks]] entry {index} must be a table"
            raise ProfileError(msg_2)
        entry = cast("dict[str, Any]", raw_entry)
        name = _require_str(entry, "name", section=f"checks[{index}]")
        if name in names:
            msg_3 = f"duplicate check name: {name!r}"
            raise ProfileError(msg_3)
        names.add(name)
        kind_name = _require_str(entry, "kind", section=f"checks[{index}]").upper()
        kind = _CHECK_KINDS.get(kind_name)
        if kind is None:
            msg_4 = f"checks[{index}].kind must be one of {tuple(_CHECK_KINDS)}, got {kind_name!r}"
            raise ProfileError(msg_4)
        argv = _require_str_list(entry, "argv", section=f"checks[{index}]")
        executable = argv[0]
        if executable == _LOCAL_PYTHON_TOKEN:
            if container:
                msg_5 = (
                    f"checks[{index}].argv uses {_LOCAL_PYTHON_TOKEN} but the profile "
                    "runs in container mode; use the in-container interpreter path"
                )
                raise ProfileError(msg_5)
            executable = str(interpreter)
        elif not container and not Path(executable).is_absolute():
            msg_6 = (
                f"checks[{index}].argv[0] must be an absolute path "
                f"(or {_LOCAL_PYTHON_TOKEN}) in local mode, got {executable!r}"
            )
            raise ProfileError(msg_6)
        timeout = _require_positive_number(entry, "timeout_seconds", section=f"checks[{index}]")
        cpu = _optional_positive_int(
            entry, "cpu_seconds", section=f"checks[{index}]", default=int(timeout)
        )
        try:
            commands.append(
                RepairCommand(
                    kind=kind,
                    name=name,
                    argv=(executable, *argv[1:]),
                    timeout_seconds=timeout,
                    cpu_seconds=cpu,
                )
            )
        except ValueError as exc:
            msg_7 = f"checks[{index}] is out of contract: {exc}"
            raise ProfileError(msg_7) from exc
    return tuple(commands)


def _require_acceptance(
    raw: dict[str, Any], checks: tuple[RepairCommand, ...]
) -> AcceptanceCriteria:
    table = _require_table(raw, "acceptance")
    required = _require_str_list(table, "required", section="acceptance")
    known = {command.name for command in checks}
    unknown = sorted(set(required) - known)
    if unknown:
        msg = f"acceptance.required names unknown checks: {unknown}"
        raise ProfileError(msg)
    prefixes = _require_str_list(table, "allowed_prefixes", section="acceptance")
    for prefix in prefixes:
        _validate_relative(prefix, field="acceptance.allowed_prefixes")
    return AcceptanceCriteria(
        required_commands=tuple(required),
        patch=PatchConstraints(
            require_change=_optional_bool(
                table, "require_change", section="acceptance", default=False
            ),
            allowed_prefixes=tuple(prefixes),
            max_changed_files=_optional_positive_int(
                table, "max_changed_files", section="acceptance", default=30
            ),
        ),
    )


def _require_approval(raw: dict[str, Any]) -> frozenset[str]:
    table = _optional_table(raw, "approval")
    value: object = table.get("required_for", [])
    if not isinstance(value, list):
        msg = "approval.required_for must be a list of tool names"
        raise ProfileError(msg)
    names: set[str] = set()
    for item in cast("list[Any]", value):
        if not isinstance(item, str) or _TOOL_NAME_PATTERN.fullmatch(item) is None:
            msg_2 = f"approval.required_for entries must be tool names, got {item!r}"
            raise ProfileError(msg_2)
        names.add(item)
    return frozenset(names)


def _require_budget(raw: dict[str, Any]) -> tuple[BudgetLimit, int | None]:
    table = _require_table(raw, "budget")
    max_cost = _require_positive_number(table, "max_cost_usd", section="budget")
    max_iterations = _optional_positive_int(table, "max_iterations", section="budget", default=0)
    if max_iterations == 0:
        msg = "budget.max_iterations must be a positive integer"
        raise ProfileError(msg)
    max_tokens_raw: object = table.get("max_total_tokens")
    max_tokens: int | None = None
    if max_tokens_raw is not None:
        max_tokens = _optional_positive_int(table, "max_total_tokens", section="budget", default=-1)
    max_elapsed_raw: object = table.get("max_elapsed_seconds")
    max_elapsed: float | None = None
    if max_elapsed_raw is not None:
        max_elapsed = _require_positive_number(table, "max_elapsed_seconds", section="budget")
    no_progress_raw: object = table.get("no_progress_limit")
    no_progress_limit: int | None = None
    if no_progress_raw is not None:
        no_progress_limit = _optional_positive_int(
            table, "no_progress_limit", section="budget", default=-1
        )
    return (
        BudgetLimit(
            max_cost_usd=max_cost,
            max_iterations=max_iterations,
            max_total_tokens=max_tokens,
            max_elapsed_seconds=max_elapsed,
        ),
        no_progress_limit,
    )
