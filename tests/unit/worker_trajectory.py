"""Scripted whole-run trajectories through the real coding composition, offline.

A trajectory is the worker's side of a run written down in advance: one scripted Ollama
reply per model turn, driven through ``build_coding`` exactly as production composes it
(``OllamaModel`` wire parsing, every model wrapper, the confined tools, the verifier, the
runtime's control and reliability policies). Only two things are replaced:

- the provider's HTTP transport, which replays the scripted replies, and
- the container sandbox, which runs each check as a local Python subprocess in the
  workspace, so acceptance probes produce real ``<trusted-acceptance>`` tracebacks.

No network and no Docker. The point is to see every stop rule act on a whole run at once,
before a live run discovers them one at a time.
"""

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from loopforge.adapters import ollama_model
from loopforge.adapters.container_sandbox import ContainerSandbox
from loopforge.adapters.git_workspace import GitWorkspace, GitWorkspaceManager
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.domain.events import RunStopped
from loopforge.domain.state import RunState
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints import campaign_coding as coding
from loopforge.entrypoints.agent_config import GATE_LIMITS, GateLimits
from loopforge.entrypoints.campaign_models import (
    AcceptanceProbe,
    CampaignConfig,
    CodingCheck,
    CodingTaskConfig,
)
from loopforge.ports.sandbox import SandboxCommandResult

GAME = "app/game.js"
FEATURES = (
    ("start", "Pressing Play must start a new game"),
    ("move", "The snake must advance one cell per tick"),
    ("turn", "Arrow keys must queue a turn"),
    ("reverse", "Immediate reversal must be rejected"),
    ("queue", "Only one turn may be queued per physics tick"),
    ("food", "Food must spawn on a free cell"),
    ("grow", "Eating food must grow the snake"),
    ("wall", "Hitting a wall must end the game"),
    ("self", "Hitting the snake's own body must end the game"),
    ("score", "The score must count eaten food"),
)


def marker(feature: str) -> str:
    return f"// feature:{feature}\n"


def probe_source(features: Sequence[tuple[str, str]] = FEATURES) -> str:
    """A linear probe: one assertion per line, stopping at the first missing feature."""
    lines = [f"source = open({GAME!r}).read()"]
    lines += [f"assert {marker(name)!r} in source, {message!r}" for name, message in features]
    return "\n".join(lines) + "\n"


def build_task(features: Sequence[tuple[str, str]] = FEATURES) -> CodingTaskConfig:
    return CodingTaskConfig(
        objective="Build the Snake gameplay in app/game.js until every acceptance check passes",
        kind="feature",
        branch="feature/snake",
        image="sha256:" + "a" * 64,
        allowed_paths=["app"],
        required_changed_paths=[GAME],
        evidence="scripted trajectory",
        checks=[
            CodingCheck(
                name="check_project",
                argv=["/usr/local/bin/python", "-c", f"open({GAME!r}).read()"],
            )
        ],
        acceptance_probes=[
            AcceptanceProbe(
                criterion_id="gameplay",
                description="Snake gameplay works in the browser",
                name="check_acceptance_gameplay",
                source=probe_source(features),
            )
        ],
        max_attempts=3,
    )


def build_workspace(root: Path) -> GitWorkspace:
    return GitWorkspaceManager(root).materialize(
        FixtureSpec(
            fixture_id="snake-starter",
            files=(
                FixtureFile(path=GAME, content="// Snake\n"),
                FixtureFile(path="app/README.md", content="Snake starter.\n"),
            ),
        )
    )


class LocalChecks(ContainerSandbox):
    """Runs each allowlisted check locally in the workspace instead of in a container."""

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        del timeout_seconds
        spec = self._commands[command_name]  # pyright: ignore[reportPrivateUsage]
        argv = [sys.executable if part == "/usr/local/bin/python" else part for part in spec.argv]
        completed = subprocess.run(
            argv,
            cwd=self._root,  # pyright: ignore[reportPrivateUsage]
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        return SandboxCommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            succeeded=completed.returncode == 0,
        )


# A scripted turn: tool calls as (name, arguments), or a function of the workspace root
# that returns them (for arguments that depend on current file content, like hashes).
Calls = tuple[tuple[str, dict[str, str]], ...]
Turn = Calls | Callable[[Path], Calls]


def call(tool: str, **arguments: str) -> Calls:
    return ((tool, arguments),)


def add_feature(feature: str) -> Turn:
    """Append one feature marker with an exact edit of the current last line."""

    def turn(root: Path) -> Calls:
        source = (root / GAME).read_text()
        last = source.splitlines(keepends=True)[-1]
        return call("edit_file", path=GAME, old=last, new=last + marker(feature))

    return turn


@dataclass
class Trajectory:
    """The scripted model side of one worker run, with per-turn prompt accounting."""

    turns: list[Turn]
    # When the script runs out, the model keeps asking for workspace status.
    idle: Calls = (("workspace_status", {}),)
    # Tokens the provider reports per turn: a fixed system prompt plus history growth,
    # the way Ollama and MLX report the whole prompt on every request.
    base_prompt_tokens: int = 4_000
    prompt_growth_tokens: int = 600
    output_tokens: int = 300
    requests: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    @property
    def consumed(self) -> int:
        return min(len(self.requests), len(self.turns))

    def transport(self, root: Path) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            index = len(self.requests) - 1
            turn = self.turns[index] if index < len(self.turns) else self.idle
            calls = turn(root) if callable(turn) else turn
            return httpx.Response(
                200,
                json={
                    "model": "qwen",
                    "done": True,
                    "prompt_eval_count": self.base_prompt_tokens
                    + self.prompt_growth_tokens * index,
                    "eval_count": self.output_tokens,
                    "message": {
                        "role": "assistant",
                        "content": "Plan: next scripted step.",
                        "tool_calls": [
                            {"function": {"name": name, "arguments": arguments}}
                            for name, arguments in calls
                        ],
                    },
                },
            )

        return httpx.MockTransport(handle)


@dataclass(frozen=True)
class Outcome:
    state: RunState
    stop_summary: str
    events: tuple[object, ...]
    turns_consumed: int
    requests: int

    @property
    def stop_code(self) -> str:
        return self.stop_summary.split(":", 1)[0]


def drive(  # noqa: PLR0913 - explicit trajectory knobs
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trajectory: Trajectory,
    *,
    task: CodingTaskConfig | None = None,
    profile: str = "qwen-native-v1",
    max_iterations: int = 128,
    max_tokens: int = 1_500_000,
    gates: GateLimits = GATE_LIMITS,
) -> Outcome:
    """Run one worker attempt to its stop and report where and why it stopped."""
    monkeypatch.setattr(coding, "ContainerSandbox", LocalChecks)
    monkeypatch.setattr(coding, "SystemSleeper", _NoSleep)
    repo = build_workspace(tmp_path / "repo")
    real = ollama_model.OllamaModel

    def factory(**kwargs: object) -> ollama_model.OllamaModel:
        return real(**{**kwargs, "transport": trajectory.transport(repo.root)})  # type: ignore[arg-type]

    monkeypatch.setattr(coding, "OllamaModel", factory)
    config = CampaignConfig(
        repository=str(repo.root),
        project="snake",
        revision=repo.base_revision,
        scope=["app"],
        rounds=1,
        inference_profile=profile,  # type: ignore[arg-type]
        model="qwen3.6:35b-a3b",
        max_iterations=max_iterations,
        max_tokens=max_tokens,
    )
    store = InMemoryEventStore()
    runtime, _ = coding.build_coding(config, task or build_task(), repo, store=store, gates=gates)
    state = runtime.run(config_objective(task or build_task()))
    events = tuple(store.events_for(state.run_id))
    stops = [event for event in events if isinstance(event, RunStopped)]
    return Outcome(
        state=state,
        stop_summary=stops[-1].summary if stops else "",
        events=events,
        turns_consumed=trajectory.consumed,
        requests=len(trajectory.requests),
    )


def config_objective(task: CodingTaskConfig) -> str:
    return task.objective


class _NoSleep:
    def sleep(self, seconds: float) -> None:
        del seconds
