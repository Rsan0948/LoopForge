"""Publish a delivered, verified goal's app and install reversible launcher wrappers.

Generalizes `.loopforge/tasks/snake/publish.py`. Refuses anything but a terminal
`delivered` goal with runtime verification; accepts it through the normal review
command (which commits the isolated branch); copies the candidate (without .git) to
apps/NAME; installs a two-line bash wrapper at ~/LAUNCHER and ~/.local/bin/LAUNCHER.
Existing files are never overwritten: a differing app or launcher stops the publish.

Usage: .venv/bin/python scripts/publish_goal_app.py GOAL_DIR NAME LAUNCHER_RELATIVE_PATH
  e.g. ... .loopforge/tasks/snake-a3b-mlx-2026-10-24 snake app/lets_play_snake
"""

import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.entrypoints.goal_review import morning_report, review_command
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL

ROOT = Path(__file__).resolve().parents[1]


def digests(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def main() -> None:
    goal, name, launcher = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
    state = SQLiteGoalJournal(goal / "goals.sqlite").load(GOAL)
    if state.phase != "delivered" or not state.observation.verified:
        msg = "Never publish an unverified or incomplete goal"
        raise SystemExit(msg)
    review = review_command(goal, "accept", None, None)
    if review.get("disposition") != "accepted":
        raise SystemExit(json.dumps(review))
    service = RepositoryGoalService(goal)
    try:
        worker = service.worker().prepared()
        assert worker is not None
        source = worker.workspace().root
        if any(p.is_symlink() for p in source.rglob("*") if ".git" not in p.parts):
            msg = "Candidate contains symlinks; not published"
            raise SystemExit(msg)
        expected = digests(source)
        target = ROOT / "apps" / name
        if target.exists():
            if digests(target) != expected:
                msg = f"{target} exists and differs; preserved for review"
                raise SystemExit(msg)
        else:
            pending = target.with_name(name + ".pending")
            if pending.exists():
                msg = f"Incomplete publication retained for inspection: {pending}"
                raise SystemExit(msg)
            shutil.copytree(source, pending, ignore=shutil.ignore_patterns(".git"))
            pending.rename(target)
        entry = target / launcher
        entry.chmod(0o755)
        wrapper = (
            f"#!/usr/bin/env bash\n# {name} launcher, managed by LoopForge ({goal.name}).\n"
            f'exec /bin/bash {shlex.quote(str(entry))} "$@"\n'
        )
        installed = []
        command = Path(launcher).name
        for path in (Path.home() / command, Path.home() / ".local/bin" / command):
            if path.exists() or path.is_symlink():
                if not path.is_file() or path.read_text() != wrapper:
                    msg = f"Existing launcher preserved: {path}"
                    raise SystemExit(msg)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                with os.fdopen(os.open(path, flags, 0o755), "w") as stream:
                    stream.write(wrapper)
            installed.append(str(path))
        (goal / "delivery-report.json").write_text(
            json.dumps(
                {
                    "report": morning_report(goal),
                    "published_app": str(target),
                    "accepted_commit": review.get("accepted_commit"),
                    "files": expected,
                    "installed_launchers": installed,
                },
                indent=2,
                default=str,
            )
        )
        print(json.dumps({"app": str(target), "launchers": installed}, indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
