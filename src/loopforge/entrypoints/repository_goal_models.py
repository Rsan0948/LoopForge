"""Trusted repository policy and preapproved environments for flexible goals."""

import hashlib
import json

from pydantic import Field, model_validator

from loopforge.domain.goal_review import ClarificationPolicy
from loopforge.entrypoints.campaign_models import CodingCheck, CodingTaskConfig, StrictRecord
from loopforge.entrypoints.supervisor import PreparedGoalConfig


class ApprovedEnvironment(StrictRecord):
    environment_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    description: str = Field(min_length=10, max_length=500)
    image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    readiness: list[CodingCheck] = Field(min_length=1, max_length=5)
    checks: list[CodingCheck] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def fixed_commands(self) -> "ApprovedEnvironment":
        names = [check.name for check in (*self.readiness, *self.checks)]
        if len(names) != len(set(names)) or any(
            not check.argv[0].startswith("/") for check in (*self.readiness, *self.checks)
        ):
            msg = "Approved environments require unique fixed commands with absolute executables"
            raise ValueError(msg)
        return self


class RepositoryGoalConfig(PreparedGoalConfig):
    """`task` is the outer authority envelope, with no preselected edit locations."""

    max_decisions: int = Field(default=48, ge=1, le=100)
    environments: list[ApprovedEnvironment] = Field(min_length=1, max_length=5)
    clarifications: tuple[ClarificationPolicy, ...] = ()
    dynamic_clarifications: tuple[ClarificationPolicy, ...] = ()
    capture_working_tree: bool = False
    max_research_failures: int = Field(default=3, ge=1, le=10)
    max_stagnant_decisions: int = Field(default=4, ge=1, le=10)

    @model_validator(mode="after")
    def investigation_policy(self) -> "RepositoryGoalConfig":
        essential = {
            "goal": self.task.objective,
            "read_scope": self.campaign.scope,
            "edit_scope": self.task.allowed_paths,
            "environments": [e.environment_id for e in self.environments],
        }
        if (
            len(json.dumps(json.dumps(essential, ensure_ascii=False), ensure_ascii=False).encode())
            > 28000
        ):
            msg = "Essential authority exceeds supervisor intake budget"
            raise ValueError(msg)
        if not self.task.acceptance_probes:
            msg = "Repository goals require independent behavioral acceptance probes"
            raise ValueError(msg)
        if len(self.clarifications) > 8 or len(self.dynamic_clarifications) > 8:
            msg = "At most eight clarification topics or categories are supported"
            raise ValueError(msg)
        ids = [environment.environment_id for environment in self.environments]
        if len(ids) != len(set(ids)):
            msg = "Approved environment IDs must be unique"
            raise ValueError(msg)
        for prefix in self.task.allowed_paths:
            if not any(
                prefix == scope or prefix.startswith(scope.rstrip("/") + "/")
                for scope in self.campaign.scope
            ):
                msg = "Edit authority must be within authorized repository scope"
                raise ValueError(msg)
        for environment in self.environments:
            CodingTaskConfig.model_validate(
                {**self.task.model_dump(), "image": environment.image, "checks": environment.checks}
            )
        return self


def repository_binding(config: RepositoryGoalConfig) -> str:
    """Preserve frozen Phase 3 intake digests when new options are unused."""
    excluded: set[str] = set()
    if not config.clarifications:
        excluded.add("clarifications")
    if not config.dynamic_clarifications:
        excluded.add("dynamic_clarifications")
    if not config.capture_working_tree:
        excluded.add("capture_working_tree")
    return hashlib.sha256(config.model_dump_json(exclude=excluded).encode()).hexdigest()
