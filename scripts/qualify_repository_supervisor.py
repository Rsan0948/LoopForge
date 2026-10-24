"""Opt-in Phase 3 live qualification: generated multi-file goal and injected failure.

Only generated fixture data may be shared. Two fixed $0.75 envelopes (original
and --clean), plus Phase 2's two $0.50 envelopes, preallocate at most $2.50 of the
existing $5 authorization. Reruns retain original deadlines, spend and failures.
"""

import argparse
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from loopforge.adapters.container_sandbox import ContainerSandbox
from loopforge.adapters.gemini_supervisor import GeminiSupervisor, load_gemini_key
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.domain.workspace import FixtureFile, FixtureSpec
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

DIRECTORY = Path(".loopforge/qualification/phase3-live")


def fixture_config(directory: Path, image: str) -> RepositoryGoalConfig:
    workspace = GitWorkspaceManager(directory / "source").materialize(
        FixtureSpec(
            fixture_id="quantity-summary",
            files=(
                FixtureFile(
                    path="src/quantities.py",
                    content=("def normalize_quantity(value):\n    return value\n"),
                ),
                FixtureFile(
                    path="src/report.py",
                    content=(
                        "from quantities import normalize_quantity\n\n"
                        "def summarize(values):\n    return {'count': len(values), 'total': 0}\n"
                    ),
                ),
                FixtureFile(path="src/api.py", content="from report import summarize\n"),
                FixtureFile(path="src/project_checks.py", content="assert True\n"),
            ),
        )
    )
    project = CodingCheck(
        name="check_project", argv=["/usr/local/bin/python", "-B", "src/project_checks.py"]
    )
    return RepositoryGoalConfig(
        campaign=CampaignConfig(
            repository=str(workspace.root.resolve()),
            project="brook",
            revision=workspace.base_revision,
            scope=["src"],
            rounds=1,
            max_iterations=32,
            max_seconds=300,
            model_timeout_seconds=120,
            inference_profile="qwen-bounded-v3",
        ),
        task=CodingTaskConfig(
            objective=(
                "Implement quantity summaries through the existing summarize(values) API. "
                "Normalize integer values and signed decimal integer strings through the shared "
                "normalization function; reject booleans, floats, empty strings and non-integer "
                "strings with ValueError. Return the count of input values and their integer "
                "total, including correct empty-input behavior. "
                "Preserve the public function signatures."
            ),
            kind="bugfix",
            branch="goal/quantity-summary",
            allowed_paths=["src"],
            image=image,
            checks=[project],
            evidence=(
                "Generated feature fixture with independent behavioral criteria; "
                "edit locations must be discovered."
            ),
            acceptance_probes=[
                AcceptanceProbe(
                    criterion_id="normalization",
                    name="check_acceptance_normalize",
                    description=(
                        "Shared normalization returns integers for signed decimal "
                        "strings and rejects invalid types/values"
                    ),
                    source=(
                        "import sys\nsys.path.insert(0, '/workspace/src')\n"
                        "from quantities import normalize_quantity as norm\n"
                        "assert norm('12') == 12\nassert norm('-3') == -3\n"
                        "assert norm('+2') == 2\nassert norm(0) == 0\n"
                        "for value in [True, False, 1.5, '', '1.5', 'x']:\n"
                        "    try: norm(value)\n    except ValueError: pass\n"
                        "    else: raise AssertionError('invalid quantity accepted')\n"
                    ),
                ),
                AcceptanceProbe(
                    criterion_id="summary",
                    name="check_acceptance_summary",
                    description=(
                        "Public summaries count inputs and total normalized quantities, "
                        "with empty input producing zero"
                    ),
                    source=(
                        "import sys\nsys.path.insert(0, '/workspace/src')\n"
                        "from api import summarize\n"
                        "assert summarize([2, '12', '-3', '+2', 0]) == {'count': 5, 'total': 13}\n"
                        "assert summarize([]) == {'count': 0, 'total': 0}\n"
                        "try: summarize([True])\nexcept ValueError: pass\n"
                        "else: raise AssertionError('invalid public input accepted')\n"
                    ),
                ),
            ],
            max_attempts=3,
            max_review_attempts=2,
            max_task_tokens=750000,
            max_task_seconds=1200,
            max_changed_files=4,
            max_review_reads=24,
        ),
        environments=[
            ApprovedEnvironment(
                environment_id="pytest",
                description=(
                    "Python environment requiring pytest; its readiness check must succeed"
                ),
                image=image,
                readiness=[
                    CodingCheck(
                        name="check_environment_pytest",
                        argv=["/usr/local/bin/python", "-B", "-c", "import pytest"],
                    )
                ],
                checks=[project],
            ),
            ApprovedEnvironment(
                environment_id="stdlib",
                description=(
                    "Python standard-library execution; no third-party dependencies required"
                ),
                image=image,
                readiness=[
                    CodingCheck(
                        name="check_environment_stdlib",
                        argv=["/usr/local/bin/python", "-B", "-c", "import unittest, json"],
                    )
                ],
                checks=[project],
            ),
        ],
        max_cost_microusd=750000,
        max_goal_seconds=1500,
        max_decisions=24,
        max_failures=3,
        max_research_failures=4,
        remote_data="synthetic-fixture",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for paid Gemini and local Qwen evaluation")
    directory = DIRECTORY.with_name("phase3-live-clean") if args.clean else DIRECTORY
    service = RepositoryGoalService(directory)
    supervisor = None
    original_run = ContainerSandbox.run
    injection_file = directory / "injected-environment-failure.json"

    def injected_run(
        sandbox: ContainerSandbox, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        if command_name.startswith("check_environment_") and not injection_file.exists():
            injection_file.write_text(
                json.dumps(
                    {"failure": "transient container backend unavailable", "command": command_name}
                )
            )
            message = "Injected transient container backend failure; no workload executed"
            raise SandboxError(message)
        return original_run(sandbox, command_name, timeout_seconds=timeout_seconds)

    try:
        intake = directory / "evaluation-intake.json"
        if intake.exists():
            config = RepositoryGoalConfig.model_validate_json(intake.read_text())
        else:
            image = subprocess.run(
                ["docker", "image", "inspect", "python:3.12-alpine", "--format", "{{.Id}}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
            config = fixture_config(directory, image)
            intake.write_text(config.model_dump_json())
        state = service.create(config)
        if state.terminal:
            print(json.dumps(asdict(state), indent=2))
            return
        ContainerSandbox.run = injected_run
        supervisor = GeminiSupervisor(load_gemini_key(Path(".loopforge/secrets.env")))
        coordinator = service.coordinator(supervisor)
        restarted = False
        while not state.terminal:
            state = coordinator.step(GOAL)
            print(
                json.dumps(
                    {
                        "phase": state.phase,
                        "worker": state.observation.status,
                        "records": len(state.research),
                        "environment": state.environment_id,
                        "baseline_reproduced": state.baseline_reproduced,
                        "spent_microusd": state.spent_microusd,
                        "summary": state.summary,
                    }
                ),
                flush=True,
            )
            if not restarted and state.baseline_reproduced and state.assignment is None:
                service.close()
                service = RepositoryGoalService(directory)
                coordinator = service.coordinator(supervisor)
                restarted = True
                print(
                    (
                        "Qualification restarted over persisted discoveries,"
                        " environment and reproduction"
                    ),
                    flush=True,
                )
            if not state.terminal:
                time.sleep(1)
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "state": asdict(state),
                    "restarted": restarted,
                    "injected_failure": injection_file.exists(),
                },
                indent=2,
            )
        )
        print(
            json.dumps(
                {
                    "result": state.phase,
                    "spent_microusd": state.spent_microusd,
                    "restarted": restarted,
                    "injected_failure": injection_file.exists(),
                }
            ),
            flush=True,
        )
    finally:
        ContainerSandbox.run = original_run
        if supervisor:
            supervisor.close()
        service.close()


if __name__ == "__main__":
    main()
