"""Network-free Phase 4 qualification using real containers and a scripted worker.

Leaves a delivered isolated fixture for browser review. No provider request or
production campaign is executed. Requires the already installed test suite and
prepared python:3.12-alpine image; never pulls an image or falls back to the host.
"""

import importlib.util
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx

from loopforge.adapters.gemini_supervisor import GeminiSupervisor
from loopforge.domain.goal_review import ClarificationPolicy
from loopforge.entrypoints.goal_checkpoint import capture
from loopforge.entrypoints.goal_review import morning_report
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL
from scripts.qualify_repository_supervisor import fixture_config

DIRECTORY = Path(".loopforge/qualification/phase4")


def main() -> None:
    image = subprocess.run(
        ["docker", "image", "inspect", "python:3.12-alpine", "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    spec = importlib.util.spec_from_file_location(
        "phase4_fixture", "tests/integration/test_repository_supervisor.py"
    )
    assert spec
    assert spec.loader
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    directory = DIRECTORY / "goals" / "absence"
    if (directory / "goals.sqlite").exists():
        msg = "Qualification directory already exists; inspect the retained result"
        raise ValueError(msg)
    config = fixture_config(DIRECTORY / "fixture", image)
    source = Path(config.campaign.repository)
    (source / "src/api.py").write_text("# Operator local change\nfrom report import summarize\n")
    (source / "src/notes.py").write_text("# Operator untracked work\n")
    before = capture(source)
    config = config.model_copy(
        update={
            "capture_working_tree": True,
            "clarifications": (
                ClarificationPolicy(
                    "presentation",
                    "Which presentation should the summary use?",
                    "optional",
                    1,
                    "Keep existing API",
                ),
            ),
        }
    )

    class Clock:
        epoch = int(datetime.now(UTC).timestamp())

        def now(self) -> datetime:
            return datetime.fromtimestamp(self.epoch, UTC)

    clock = Clock()
    service = RepositoryGoalService(directory)
    try:
        state = service.create(config)
        service.clock = clock
        service.harness.coding.driver = fixture.driver
        with httpx.Client(transport=httpx.MockTransport(fixture.provider)) as client:
            supervisor = GeminiSupervisor("offline-fixture", client=client)
            coordinator = service.coordinator(supervisor)
            state = coordinator.step(GOAL)
            clock.epoch = state.questions[0].deadline_epoch
            for step in range(50):
                state = coordinator.step(GOAL)
                if step == 8:
                    service.close()
                    service = RepositoryGoalService(directory)
                    service.clock = clock
                    service.harness.coding.driver = fixture.driver
                    coordinator = service.coordinator(supervisor)
                if state.terminal:
                    break
        assert state.phase == "delivered", state.summary
        assert state.observation.verified
        assert capture(source) == before
        assert len(state.experiments) == 1
        report = morning_report(directory)
        report["qualification"] = {
            "paid_provider_calls": 0,
            "actual_provider_cost_microusd": 0,
            "injected_usage_accounting_microusd": state.spent_microusd,
            "source_preserved": True,
            "restart_exercised": True,
        }
        (DIRECTORY / "morning-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report["qualification"], indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
