"""Real container checks, misleading editable tests, fresh review, and local acceptance."""

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from loopforge.adapters.acceptance_receipts import decode_acceptance
from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.scripted import ScriptedModel
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import VerificationFailed
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunId
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints.campaign import CampaignHarness
from loopforge.entrypoints.campaign_coding import ConfinedCodingTools
from loopforge.entrypoints.campaign_models import (
    AcceptanceProbe,
    CampaignConfig,
    CodingCheck,
    CodingCommand,
    CodingTaskConfig,
)
from loopforge.entrypoints.campaign_review import ReviewTools


@pytest.fixture
def image() -> str:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "python:3.12-alpine", "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker image inspection unavailable; no image pull or host fallback")
    if result.returncode != 0:
        pytest.skip("Prepared python:3.12-alpine image unavailable; no automatic pull")
    return result.stdout.strip()


@pytest.fixture
def harnesses() -> Iterator[list[CampaignHarness]]:
    instances: list[CampaignHarness] = []
    try:
        yield instances
    finally:
        for instance in instances:
            instance.close()


def driver(runtime: Runtime, run_id: RunId) -> RunState:
    if isinstance(runtime.tools, ConfinedCodingTools):
        runtime.model = ScriptedModel(
            [
                ActionProposal(
                    ActionId("wrong-patch"),
                    "write_file",
                    {"path": "src/add.py", "content": "def add(a,b): return -1\n"},
                ),
                ActionProposal(
                    ActionId("early-exit"),
                    "write_file",
                    {"path": "src/add.py", "content": "import os\nos._exit(0)\n"},
                ),
                ActionProposal(
                    ActionId("correct-patch"),
                    "write_file",
                    {"path": "src/add.py", "content": "def add(a,b): return a+b\n"},
                ),
            ]
        )
        checkpoint = runtime.step(run_id)
        assert not checkpoint.status.is_terminal
        assert checkpoint.last_verification_passed is False
        # The editable test passes for the wrong implementation. The
        # independent probe, whose source is not in this checkout, denies it.
        failure = next(
            e for e in runtime.store.events_for(run_id) if isinstance(e, VerificationFailed)
        )
        assert "command:check_project: passed" in failure.summary
        assert "command:check_acceptance_integer_addition: failed" in failure.summary
        early_exit = runtime.step(run_id)
        assert not early_exit.status.is_terminal
        assert early_exit.last_verification is not None
        assert "completion marker missing" in early_exit.last_verification
    else:
        assert isinstance(runtime.tools, ReviewTools)
        paths = sorted(runtime.tools.files)
        runtime.model = ScriptedModel(
            [
                *(
                    ActionProposal(ActionId(f"read-{i}"), "read_file", {"path": path})
                    for i, path in enumerate(paths)
                ),
                ActionProposal(
                    ActionId("report"),
                    "submit_report",
                    {
                        "report": json.dumps(
                            {
                                "summary": "Actual diff and source inspected; fixture verified",
                                "findings": [],
                                "unresolved": [],
                            }
                        )
                    },
                ),
            ]
        )
    return runtime.resume(run_id)


def test_trusted_acceptance_survives_restart_and_requires_fresh_review(
    tmp_path: Path,
    image: str,
    harnesses: list[CampaignHarness],
) -> None:
    original = GitWorkspaceManager(tmp_path / "source").materialize(
        FixtureSpec(
            fixture_id="prepared-acceptance",
            files=(
                FixtureFile(path="src/add.py", content="def add(a,b): return 0\n"),
                FixtureFile(path="src/add.test.py", content="assert True\n"),
            ),
        )
    )
    config = CampaignConfig(
        repository=str(original.root),
        project="brook",
        revision=original.base_revision,
        scope=["src"],
        rounds=1,
        max_iterations=12,
        max_seconds=120,
    )
    contract = CodingTaskConfig(
        objective="Repair integer addition while preserving the public add function",
        kind="bugfix",
        branch="test/trusted-acceptance",
        allowed_paths=["src"],
        required_changed_paths=["src/add.py"],
        image=image,
        evidence="independent failing arithmetic",
        checks=[
            CodingCheck(
                name="check_project", argv=["/usr/local/bin/python", "-B", "src/add.test.py"]
            )
        ],
        acceptance_probes=[
            AcceptanceProbe(
                criterion_id="integer_addition",
                name="check_acceptance_integer_addition",
                description="Integer addition must work for positive, negative, and zero values",
                source="import sys\nsys.path.insert(0, '/workspace/src')\nfrom add import add\n"
                "assert add(2, 3) == 5\nassert add(-4, 4) == 0\nassert add(0, 0) == 0\n",
            )
        ],
    )
    directory = tmp_path / "ledger"
    harness = CampaignHarness(directory)
    harnesses.append(harness)
    harness.acquire_service()
    campaign = harness.create(config)
    harness.coding.driver = driver
    harness.coding.enqueue(campaign, "acceptance_task", contract)
    assert harness.tick(campaign)["accepted"]
    assert harness.worker is not None
    harness.worker.join(60)
    assert not harness.worker.is_alive()
    with harness.store.connect() as db:
        assert harness.coding.jobs.state(db, "acceptance_task").status == "review_pending"
        row = db.execute(
            "SELECT detail FROM coding_events WHERE task=? "
            "AND json_extract(event,'$.kind')='patched'",
            ("acceptance_task",),
        ).fetchone()
        coding_detail = json.loads(row["detail"])
    coding_receipt = decode_acceptance(json.dumps(coding_detail["acceptance_receipt"]))

    # Rebuild composition over the durable ledger; the worker run remains terminal.
    harness.close()
    recovered = CampaignHarness(directory)
    harnesses.append(recovered)
    recovered.acquire_service()
    recovered.coding.driver = driver
    assert recovered.tick(campaign)["accepted"]
    assert recovered.worker is not None
    recovered.worker.join(60)
    assert not recovered.worker.is_alive()
    with recovered.store.connect() as db:
        assert recovered.coding.jobs.state(db, "acceptance_task").status == "patch_ready"
        row = db.execute(
            "SELECT detail FROM coding_events WHERE task=? "
            "AND json_extract(event,'$.kind')='reviewed'",
            ("acceptance_task",),
        ).fetchone()
        review_detail = json.loads(row["detail"])
    review_receipt = decode_acceptance(json.dumps(review_detail["acceptance_receipt"]))
    assert review_receipt.run_id != coding_receipt.run_id
    assert review_receipt.candidate == coding_receipt.candidate
    assert review_receipt.contract_sha256 == coding_receipt.contract_sha256
    candidate = directory / "coding" / "acceptance_task" / "src/add.py"
    verified_source = candidate.read_text()
    candidate.write_text("def add(a,b): return a-b\n")
    with pytest.raises(ValueError, match="stale"):
        recovered.coding.command(
            campaign,
            "acceptance_task",
            CodingCommand(command="accept", evidence="stale candidate must be denied"),
        )
    candidate.write_text(verified_source)
    accepted = recovered.coding.command(
        campaign,
        "acceptance_task",
        CodingCommand(command="accept", evidence="fresh independent receipt and review inspected"),
    )
    assert accepted["status"] == "accepted"
    assert (original.root / "src/add.py").read_text() == "def add(a,b): return 0\n"
