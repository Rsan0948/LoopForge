"""Consistent local SQLite backup and isolated restore qualification. No dispatch."""

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import httpx

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.domain.state import replay
from loopforge.domain.types import RunId
from loopforge.entrypoints.campaign_coding import local_git, local_git_bytes
from loopforge.entrypoints.campaign_store import CampaignStore


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024**2), b""):
            value.update(block)
    return value.hexdigest()


def sqlite_backup(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(destination)) as copy,
    ):
        original.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            msg = "Backup failed integrity check"
            raise RuntimeError(msg)
    destination.chmod(0o600)


def main():  # noqa: PLR0912, PLR0915 - backup and isolated restore stages
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".loopforge/campaign"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with httpx.Client(timeout=10) as client:
        campaigns = (
            client.get("http://localhost:8124/campaigns").raise_for_status().json()["campaigns"]
        )
    if any(c["state"]["status"] != "parked" or c["slot"] or c["worker_alive"] for c in campaigns):
        msg = "Campaigns must be parked and idle for a coordinated backup"
        raise RuntimeError(msg)
    args.output.mkdir(parents=True, exist_ok=False)
    args.output.chmod(0o700)
    for source in (
        args.data / "campaigns.sqlite",
        args.data / "runs.sqlite",
        Path.home() / "ai-platform/automation_reports.db",
    ):
        sqlite_backup(source, args.output / source.name)
    for source in args.data.glob("*.snapshot.json"):
        shutil.copyfile(source, args.output / source.name)
        (args.output / source.name).chmod(0o600)
    original = CampaignStore(args.data / "campaigns.sqlite")
    restored = CampaignStore(args.output / "campaigns.sqlite")
    states = {}
    with original.connect() as source, restored.connect() as copy:
        ids = [row["id"] for row in source.execute("SELECT id FROM campaigns")]
        for campaign in ids:
            before, after = original.state(source, campaign), restored.state(copy, campaign)
            if before != after:
                msg = "Restored reducer state differs from original"
                raise RuntimeError(msg)
            states[campaign] = {"status": after.status, "cursor": after.cursor}
        task_ids = [row["id"] for row in source.execute("SELECT id FROM coding_tasks")]
    # Preserve exact uncommitted candidate patches and added files. No candidate
    # content is executed during backup or restore verification.
    candidates = []
    for task in task_ids:
        root = args.data / "coding" / task
        if not (root / ".git").exists():
            continue
        destination = args.output / "candidates" / task
        destination.mkdir(parents=True)
        destination.chmod(0o700)
        (destination / "patch.diff").write_bytes(
            local_git_bytes(root, "diff", "--binary", "HEAD", "--")
        )
        head = local_git(root, "rev-parse", "HEAD")
        local_git(root, "bundle", "create", str((destination / "commits.bundle").resolve()), "HEAD")
        added = (
            local_git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
            .decode("utf-8")
            .split("\0")
        )
        files = {}
        for name in added:
            if not name:
                continue
            path = root / name
            if path.is_symlink() or not path.is_file():
                msg = "Unsupported candidate file type; backup incomplete"
                raise RuntimeError(msg)
            target = destination / "added" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o600)
            files[name] = {"sha256": digest(target), "mode": path.stat().st_mode & 0o777}
        (destination / "identity.json").write_text(
            json.dumps({"head": head, "added": files}, indent=2)
        )
        candidates.append(task)
    events = SQLiteEventStore(args.output / "runs.sqlite", codec=JsonEventCodec())
    with restored.connect() as copy:
        runs = [
            json.loads(row["detail"])["run_id"]
            for row in copy.execute(
                "SELECT detail FROM campaign_events WHERE json_type(detail,'$.run_id')='text' "
                "ORDER BY sequence DESC LIMIT 10"
            )
        ]
    for run in set(runs):
        replay(RunId(run), events.events_for(RunId(run)))
    with tempfile.TemporaryDirectory(prefix="loopforge-restore-") as temporary:
        local_git(Path(temporary), "init")
        for task in candidates:
            path = args.output / "candidates" / task
            # Verify all bundled commits/objects without executing repository code.
            local_git(Path(temporary), "bundle", "verify", str((path / "commits.bundle").resolve()))
    for path in args.output.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    manifest = {
        "states": states,
        "candidate_bundles": candidates,
        "replayed_runs": len(set(runs)),
        "files": {
            str(p.relative_to(args.output)): digest(p)
            for p in args.output.rglob("*")
            if p.is_file()
        },
        "limitations": "SQLite integrity, reducer replay and bundle object verification; "
        "no restored application execution or campaign dispatch",
    }
    (args.output / "receipt.json").write_text(json.dumps(manifest, indent=2))
    (args.output / "receipt.json").chmod(0o600)
    print(
        json.dumps(
            {
                "backup": str(args.output),
                "states": states,
                "candidate_bundles": len(candidates),
                "replayed_runs": len(set(runs)),
            }
        )
    )


if __name__ == "__main__":
    main()
