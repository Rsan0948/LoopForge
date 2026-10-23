"""Opt-in paid Phase 2 qualification using only a generated arithmetic fixture.

Run from the repository root with `.venv/bin/python scripts/qualify_supervisor.py
--live`. One fixed durable ledger enforces a $0.50 goal ceiling inside the
existing $5 authorization. No production repository material is sent to Gemini.
"""

import argparse
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from loopforge.adapters.gemini_supervisor import GeminiSupervisor, load_gemini_key
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints.campaign_models import (
    AcceptanceProbe,
    CampaignConfig,
    CodingCheck,
    CodingTaskConfig,
)
from loopforge.entrypoints.supervisor import GOAL, PreparedGoalConfig, PreparedGoalService

DIRECTORY = Path(".loopforge/qualification/phase2-live")


def fixture_config(directory: Path) -> PreparedGoalConfig:
    image = subprocess.run(
        ["docker", "image", "inspect", "python:3.12-alpine", "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    workspace = GitWorkspaceManager(directory / "source").materialize(
        FixtureSpec(
            fixture_id="synthetic-addition",
            files=(
                FixtureFile(path="src/add.py", content="def add(a, b):\n    return 0\n"),
                FixtureFile(path="src/project.test.py", content="assert True\n"),
            ),
        )
    )
    return PreparedGoalConfig(
        campaign=CampaignConfig(
            repository=str(workspace.root.resolve()),
            project="brook",
            revision=workspace.base_revision,
            scope=["src"],
            rounds=1,
            max_iterations=24,
            max_seconds=240,
            model_timeout_seconds=120,
            inference_profile="qwen-coding-v1",
        ),
        task=CodingTaskConfig(
            objective=(
                "Repair the public add(a, b) function so it returns integer addition for "
                "positive, negative and zero operands. Preserve its signature. "
                "Deliver a minimal verified candidate."
            ),
            kind="bugfix",
            branch="goal/synthetic-addition",
            allowed_paths=["src/add.py"],
            required_changed_paths=["src/add.py"],
            image=image,
            evidence=(
                "Synthetic fixture: add(2, 3) currently returns 0; required result is 5. "
                "The editable project test is deliberately shallow; independent arithmetic "
                "checks are authoritative."
            ),
            checks=[
                CodingCheck(
                    name="check_project",
                    argv=["/usr/local/bin/python", "-B", "src/project.test.py"],
                )
            ],
            acceptance_probes=[
                AcceptanceProbe(
                    criterion_id="integer_addition",
                    name="check_acceptance_addition",
                    description="Positive, negative and zero operands yield their integer sum",
                    source=(
                        "import sys\nsys.path.insert(0, '/workspace/src')\nfrom add import add\n"
                        "assert add(2,3)==5\nassert add(-4,4)==0\nassert add(0,0)==0\n"
                        "assert add(-5,-2)==-7\n"
                    ),
                )
            ],
            max_attempts=2,
            max_review_attempts=2,
            max_task_tokens=500000,
            max_task_seconds=900,
            max_review_reads=8,
            max_changed_files=1,
        ),
        max_cost_microusd=500000,
        max_goal_seconds=900,
        max_decisions=8,
        max_failures=2,
        remote_data="synthetic-fixture",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Use Gemini and the existing local Qwen worker"
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Use the second fixed $0.50 envelope for a clean qualification",
    )
    args = parser.parse_args()
    directory = DIRECTORY.with_name("phase2-live-clean") if args.clean else DIRECTORY
    if not args.live:
        parser.error("--live is required; this performs paid inference on synthetic data only")
    service = PreparedGoalService(directory)
    supervisor = None
    try:
        intake = directory / "evaluation-intake.json"
        if intake.exists():
            config = PreparedGoalConfig.model_validate_json(intake.read_text())
        else:
            config = fixture_config(directory)
            intake.write_text(config.model_dump_json())
        state = service.create(config)
        if state.terminal:
            print(json.dumps(asdict(state), indent=2))
            return
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
                        "spent_microusd": state.spent_microusd,
                        "reserved_microusd": state.reserved_microusd,
                        "summary": state.summary,
                    }
                ),
                flush=True,
            )
            if not restarted and state.observation.status == "review_pending":
                # No chat acknowledgement: composition is rebuilt automatically
                # between terminal worker run and independent fresh review.
                service.close()
                service = PreparedGoalService(directory)
                coordinator = service.coordinator(supervisor)
                restarted = True
                print(
                    "Qualification service restarted over persisted goal and worker ledgers",
                    flush=True,
                )
            if not state.terminal:
                time.sleep(1)
        (directory / "result.json").write_text(
            json.dumps({"state": asdict(state), "restarted": restarted}, indent=2)
        )
        print(
            json.dumps(
                {
                    "result": state.phase,
                    "spent_microusd": state.spent_microusd,
                    "restarted": restarted,
                }
            ),
            flush=True,
        )
    finally:
        if supervisor is not None:
            supervisor.close()
        service.close()


if __name__ == "__main__":
    main()
