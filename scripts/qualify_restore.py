"""Restore backed-up ledgers and candidate bundles into an isolated API copy.

Only trusted local Git/SQLite and read-only ASGI requests run. No candidate code,
checks, model calls or campaign dispatch is executed.
"""

import argparse
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from loopforge.entrypoints.campaign import create_app
from loopforge.entrypoints.campaign_coding import local_git, local_git_bytes
from scripts.backup_campaign import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backup, output = args.backup.resolve(), args.output.resolve()
    manifest = json.loads((backup / "receipt.json").read_text())
    for name, expected in manifest["files"].items():
        if digest(backup / name) != expected:
            message = "Backup digest mismatch: " + name
            raise RuntimeError(message)
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    for name in ("campaigns.sqlite", "runs.sqlite", "automation_reports.db"):
        shutil.copyfile(backup / name, output / name)
        (output / name).chmod(0o600)
    restored = []
    for task in manifest["candidate_bundles"]:
        source = backup / "candidates" / task
        root = output / "coding" / task
        root.parent.mkdir(parents=True, exist_ok=True)
        identity = json.loads((source / "identity.json").read_text())
        local_git(
            root.parent,
            "clone",
            "--no-hardlinks",
            "--no-checkout",
            "--",
            str(source / "commits.bundle"),
            str(root),
        )
        local_git(root, "checkout", "--detach", identity["head"])
        if (source / "patch.diff").stat().st_size:
            local_git(root, "apply", "--binary", str(source / "patch.diff"))
        for name, meta in identity["added"].items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                message = "Unsafe restored file path"
                raise RuntimeError(message)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / "added" / relative, target)
            target.chmod(meta["mode"])
            if digest(target) != meta["sha256"]:
                message = "Restored added file digest mismatch"
                raise RuntimeError(message)
        if (
            local_git_bytes(root, "diff", "--binary", "HEAD", "--")
            != (source / "patch.diff").read_bytes()
        ):
            message = "Restored candidate diff differs: " + task
            raise RuntimeError(message)
        restored.append(task)
    checkpoints = {}
    with TestClient(create_app(output)) as client:
        client.get("/health").raise_for_status()
        for campaign, expected in manifest["states"].items():
            checkpoint = (
                client.get("/campaigns/" + campaign + "/checkpoint").raise_for_status().json()
            )
            assert {k: checkpoint["state"][k] for k in ("status", "cursor")} == expected
            assert checkpoint["slot"] is None
            assert not checkpoint["worker_alive"]
            checkpoints[campaign] = checkpoint
    receipt = {
        "passed": True,
        "restored_candidates": restored,
        "checkpoints": checkpoints,
        "limitations": "Read-only restored API, exact Git patches/added files and ledger state; "
        "no repository workload execution, dispatch or production restore.",
    }
    (output / "restore-result.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps({"passed": True, "candidates": len(restored), "campaigns": len(checkpoints)}))


if __name__ == "__main__":
    main()
