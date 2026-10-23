"""Operator console API; server-owned roots and explicit same-origin mutations."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from loopforge.entrypoints.goal_review import morning_report, review_command
from loopforge.entrypoints.goal_runner import GoalExecutionService, runner_record
from loopforge.entrypoints.goal_successor import prepare_successor


class GoalCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str = Field(pattern=r"^(start|pause|resume|cancel|answer|accept|discard|successor)$")
    topic: str | None = Field(default=None, max_length=80)
    answer: str | None = Field(default=None, max_length=2000)


def goal_router(root: Path, execution: GoalExecutionService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/goals")
    root = root.resolve()

    def directory(identity: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity):
            raise HTTPException(404, "Unknown goal")
        path = root / identity
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or not (path / "goals.sqlite").is_file()
        ):
            raise HTTPException(404, "Unknown goal")
        return path

    @router.get("")
    def _goals() -> list[dict[str, object]]:
        if not root.exists():
            return []
        results: list[dict[str, object]] = []
        for path in sorted(root.iterdir()):
            if path.is_dir() and not path.is_symlink() and (path / "goals.sqlite").is_file():
                report = morning_report(directory(path.name))
                results.append(
                    {
                        "id": path.name,
                        "phase": report["phase"],
                        "execution_status": report["execution_status"],
                        "disposition": report["disposition"],
                        "spent_microusd": report["paid_spent_microusd"],
                        "runner": report["runner"],
                    }
                )
        return results

    @router.get("/{identity}")
    def _detail(identity: str) -> dict[str, object]:
        result = morning_report(directory(identity))
        result["runner"] = runner_record(directory(identity))
        return result

    @router.post("/{identity}/commands")
    def _command(identity: str, body: GoalCommand, request: Request) -> dict[str, object]:
        origin = request.headers.get("origin")
        if request.headers.get("x-loopforge-operator") != "1" or (
            origin and origin.rstrip("/") != str(request.base_url).rstrip("/")
        ):
            raise HTTPException(403, "Explicit same-origin operator request required")
        try:
            if body.command == "start":
                if execution is None:
                    msg = "Execution service is unavailable"
                    raise ValueError(msg)  # noqa: TRY301 - explicit boundary dispatch/normalization
                execution.arm(directory(identity))
                return {"runner": runner_record(directory(identity))}
            if body.command == "successor":
                return prepare_successor(directory(identity))
            result = review_command(directory(identity), body.command, body.topic, body.answer)
            if (
                body.command == "resume"
                and execution is not None
                and runner_record(directory(identity))["armed"]
            ):
                execution.start()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        else:
            return result

    # FastAPI retains the decorated handlers.
    _ = (_goals, _detail, _command)
    return router
