"""Freeze references to validated local comparison inputs; no inference or execution."""

import hashlib
import json
import subprocess
from pathlib import Path

from loopforge.entrypoints.campaign_coding import preflight
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / ".loopforge/mlx-lab"
TRIALS = ROOT / ".loopforge/tasks/reconciliation-forced-attempt"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    LAB.mkdir(parents=True, exist_ok=True)
    tasks = []
    for trial, label in [
        ("rich-session-2026-10-23", "api-status-and-multifile-repair"),
        ("full-a3b-2026-10-23", "tests-and-responsive-layout"),
        ("review-coverage-2026-10-23", "passing-baseline-coverage-and-recovery"),
    ]:
        path = TRIALS / trial / "repository-intake.json"
        config = RepositoryGoalConfig.model_validate_json(path.read_text())
        preflight(config.task)  # Existing pinned-image and Docker readiness checks.
        source = Path(config.campaign.repository)
        revision = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if revision != config.campaign.revision:
            msg = "Preserved task revision differs from frozen intake"
            raise ValueError(msg)
        tasks.append(
            {
                "name": label,
                "intake": str(path),
                "intake_sha256": digest(path),
                "source": str(source),
                "revision": revision,
                "intent": config.task.kind,
                "image": config.task.image,
                "sandbox_max_open_files": config.task.sandbox_max_open_files,
                "fixed_checks": [c.name for c in config.task.checks],
                "independent_probes": [p.criterion_id for p in config.task.acceptance_probes],
                "historical_evidence": str(TRIALS / trial / "worker/runs.sqlite"),
                "source_files": {
                    str(f.relative_to(source)): digest(f)
                    for f in sorted(source.rglob("*"))
                    if f.is_file() and ".git" not in f.relative_to(source).parts
                },
            }
        )
    # Keep real native conversation payloads, not reconstructed prompt summaries.
    inputs = []
    for trial in ["full-a3b-2026-10-23", "review-coverage-2026-10-23"]:
        requests = list((TRIALS / trial / "worker-prompts").glob("*.json"))
        ordered = sorted(requests, key=lambda p: len(json.loads(p.read_text())["messages"]))
        selected = [ordered[i] for i in sorted({0, len(ordered) // 2, len(ordered) - 1})]
        for path in selected:
            data = json.loads(path.read_text())
            inputs.append(
                {
                    "path": str(path),
                    "sha256": digest(path),
                    "bytes": path.stat().st_size,
                    "messages": len(data["messages"]),
                    "tools": len(data.get("tools", [])),
                    "model": data["model"],
                    "thinking": data.get("think"),
                    "options": data["options"],
                    "trial": trial,
                }
            )
    result = {
        "tasks": tasks,
        "saved_requests": inputs,
        "ollama_model": "qwen3.6:35b-a3b",
        "mlx_model": "mlx-community/Qwen3.6-35B-A3B-4bit",
        "network_or_inference_calls": False,
        "preflight": (
            "Existing pinned-image/Docker availability checks passed; no candidate checks rerun"
        ),
        "proposed_limits": {
            "repetitions": 2,
            "engines": 2,
            "tasks": 3,
            "seconds_per_trial": 600,
            "turns_per_trial": 24,
            "output_tokens_per_turn": 4096,
        },
        "limitations": [
            "Different quantizations; do not attribute every difference to engine performance",
            "Historical Ollama timings are reference data; matched fresh controls are still needed",
            "No replay or coding trials have been launched by this preparation script",
            "A new paired runner must validate these hashes before using disposable copies",
        ],
    }
    (LAB / "comparison-inputs.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"tasks": len(tasks), "requests": len(inputs), "inference_calls": 0}))


if __name__ == "__main__":
    main()
