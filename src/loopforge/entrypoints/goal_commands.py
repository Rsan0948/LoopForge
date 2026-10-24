"""Command lines for prepared and repository goals.

``run`` steps a goal in this process through the shared composition in
``goal_bootstrap``; ``launch`` arms a dedicated runner for one goal.
"""

import argparse
import json
import threading
from dataclasses import asdict
from pathlib import Path

from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.entrypoints.goal_bootstrap import GoalWiring, drive_goal
from loopforge.entrypoints.goal_launch import launch
from loopforge.entrypoints.goal_review import review_command
from loopforge.entrypoints.repository_goal_models import RepositoryGoalConfig
from loopforge.entrypoints.repository_supervisor import RepositoryGoalService
from loopforge.entrypoints.supervisor import GOAL, PreparedGoalConfig, PreparedGoalService

REVIEW_COMMANDS = ("pause", "resume", "answer", "report", "accept", "discard")


def repository_main() -> None:
    parser = argparse.ArgumentParser(description="Repository goal supervisor")
    parser.add_argument(
        "command", choices=("create", "run", "status", "cancel", *REVIEW_COMMANDS, "launch")
    )
    parser.add_argument("--directory", type=Path, default=Path(".loopforge/repository-supervisor"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--topic")
    parser.add_argument("--answer")
    args = parser.parse_args()
    if args.command in REVIEW_COMMANDS:
        print(
            json.dumps(
                review_command(args.directory, args.command, args.topic, args.answer), indent=2
            )
        )
        return
    if args.command == "launch":
        print(json.dumps(launch(args.directory, args.config), indent=2))
        return
    if args.command == "status":
        print(
            json.dumps(
                asdict(SQLiteGoalJournal(args.directory / "goals.sqlite").load(GOAL)), indent=2
            )
        )
        return
    if args.command == "run":
        print(json.dumps(run(args.directory, args.key_file), indent=2))
        return
    service = RepositoryGoalService(args.directory)
    try:
        if args.command == "create":
            if args.config is None:
                parser.error("create requires --config with a trusted repository policy")
            state = service.create(
                RepositoryGoalConfig.model_validate_json(args.config.read_text())
            )
        else:
            state = service.cancel()
        print(json.dumps(asdict(state), indent=2))
    finally:
        service.close()


def prepared_main() -> None:
    parser = argparse.ArgumentParser(description="Prepared goal supervisor")
    parser.add_argument(
        "command",
        choices=(
            "create",
            "run",
            "status",
            "cancel",
            "pause",
            "resume",
            "report",
            "accept",
            "discard",
        ),
    )
    parser.add_argument("--directory", type=Path, default=Path(".loopforge/supervisor"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    if args.command in {"pause", "resume", "report", "accept", "discard"}:
        print(json.dumps(review_command(args.directory, args.command), indent=2))
        return
    if args.command == "status":
        print(
            json.dumps(
                asdict(SQLiteGoalJournal(args.directory / "goals.sqlite").load(GOAL)), indent=2
            )
        )
        return
    if args.command == "run":
        print(json.dumps(run(args.directory, args.key_file), indent=2))
        return
    service = PreparedGoalService(args.directory)
    try:
        if args.command == "create":
            if args.config is None:
                parser.error("create requires --config with an operator-prepared contract")
            state = service.create(PreparedGoalConfig.model_validate_json(args.config.read_text()))
        else:
            state = service.cancel()
        print(json.dumps(asdict(state), indent=2))
    finally:
        service.close()


def run(directory: Path, key_file: Path | None) -> dict[str, object]:
    """Step the goal in this process until it is terminal or needs a person."""
    outcome = drive_goal(directory, threading.Event(), GoalWiring(key_file=key_file))
    result: dict[str, object] = asdict(SQLiteGoalJournal(directory / "goals.sqlite").load(GOAL))
    result["waiting"] = outcome == "waiting"
    return result
