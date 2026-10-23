"""Offline end-to-end goal proof: scripted models, real containers, real gates.

No Gemini or Ollama calls. Proves whether a known-correct patch can travel
prepare -> reproduce -> inspect -> delegate -> worker -> verification -> review
-> deliver through the real gates of a frozen repository intake.

Usage:
  .venv/bin/python scripts/prove_goal_gates.py TRIAL_DIR FIXED_TREE OUT_DIR [PROFILE]

TRIAL_DIR holds repository-intake.json; FIXED_TREE holds the corrected FILES;
OUT_DIR is created fresh. The intake copy raises sandbox_max_open_files to 1024
and selects PROFILE (default qwen-native-v1).
"""

import json
import shutil
import sys
from pathlib import Path

import httpx

from loopforge.adapters.gemini_supervisor import GeminiSupervisor
from loopforge.adapters.scripted import ScriptedModel
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, RunId
from loopforge.entrypoints.campaign_coding import ConfinedCodingTools
from loopforge.entrypoints.campaign_review import ReviewTools
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL

FILES = [
    "app/server.py",
    "app/tests/test_api.py",
    "app/tests/test_calculations.py",
    "app/static/styles.css",
]
log: list[str] = []


def provider(request: httpx.Request) -> httpx.Response:
    context = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"])
    inv = context["investigation_state"]
    worker = context["worker_evidence_untrusted"]
    sources = [m["id"] for m in inv["memory"] if m["kind"] == "source"]
    baseline = [m["id"] for m in inv["memory"] if m["kind"] == "baseline"]
    action: dict[str, object]
    if not inv["environment_ready"]:
        action = {"action": "prepare", "environment": "reconciliation"}
    elif not inv["baseline_reproduced"]:
        action = {"action": "reproduce"}
    elif not sources:
        action = {"action": "inspect", "inspection": {"path": "app/server.py"}}
    elif inv["assignment"] is None:
        assignment = {
            "objective": "Fix re-upload status, two wrong test expectations and mobile overflow",
            "files": FILES,
            "evidence_ids": baseline + sources,
        }
        action = {"action": "delegate", "assignment": assignment}
    elif worker["status"] == "review_pending":
        action = {"action": "verify"}
    elif worker["verified"]:
        action = {"action": "deliver"}
    else:
        action = {"action": "block"}
    log.append(f"supervisor -> {action['action']} (worker={worker['status']})")
    decision = {**action, "rationale": "scripted offline gate proof", "plan": "gate proof"}
    usage = {
        "promptTokenCount": 100,
        "candidatesTokenCount": 50,
        "thoughtsTokenCount": 0,
        "totalTokenCount": 150,
    }
    candidate = {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(decision)}]}}
    return httpx.Response(
        200, json={"modelVersion": "offline", "usageMetadata": usage, "candidates": [candidate]}
    )


def make_driver(fixed: Path):
    def driver(runtime: Runtime, run_id: RunId) -> RunState:
        if isinstance(runtime.tools, ConfinedCodingTools):
            log.append("worker: coding run")
            runtime.model = ScriptedModel(
                [
                    ActionProposal(
                        ActionId(f"w{i}"),
                        "write_file",
                        {"path": path, "content": (fixed / path).read_text()},
                    )
                    for i, path in enumerate(FILES)
                ]
            )
        else:
            assert isinstance(runtime.tools, ReviewTools), type(runtime.tools)
            # A reviewer must page through every changed hunk and the full diff.
            reads: list[ActionProposal] = []
            for path in [*FILES, "CAMPAIGN_PATCH.diff"]:
                lines = runtime.tools.files[path].count("\n") + 2
                for start in range(1, max(2, lines), 40):
                    reads.append(
                        ActionProposal(
                            ActionId(f"r{len(reads)}"),
                            "read_file",
                            {"path": path, "start_line": str(start)},
                        )
                    )
            report = {
                "summary": "Patch inspected; statuses, test expectations and CSS match contract",
                "findings": [],
                "unresolved": [],
            }
            submit = ActionProposal(
                ActionId("report"), "submit_report", {"report": json.dumps(report)}
            )
            runtime.model = ScriptedModel([*reads, submit])
            log.append(f"review: {len(reads)} paged reads")
        state = runtime.resume(run_id)
        log.append(f"  run {run_id}: {state.status.value} :: {state.last_verification[:200]}")
        return state

    return driver


def main() -> None:
    trial, fixed, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    profile = sys.argv[4] if len(sys.argv) > 4 else "qwen-native-v1"
    shutil.rmtree(out, ignore_errors=True)
    raw = json.loads((trial / "repository-intake.json").read_text())
    raw["task"]["sandbox_max_open_files"] = 1024
    raw["campaign"]["inference_profile"] = profile
    config = RepositoryGoalConfig.model_validate_json(json.dumps(raw))
    service = RepositoryGoalService(out)
    state = service.journal.load(GOAL)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        supervisor = GeminiSupervisor("fixture-key", client=client)
        try:
            service.create(config)
            service.harness.coding.driver = make_driver(fixed)
            coordinator = service.coordinator(supervisor)
            for _ in range(40):
                state = coordinator.step(GOAL)
                if state.terminal:
                    break
                worker = service.harness.worker
                if state.observation.status in {"coding", "reviewing", "queued"} and worker:
                    worker.join()
        finally:
            print("\n".join(log))
            print("FINAL", state.phase, "|", state.summary)
            service.close()


if __name__ == "__main__":
    main()
