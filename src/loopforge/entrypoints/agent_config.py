"""The agent's configuration in one place: where it lives, what each worker profile does,
and the limits that stop a run.

Nothing here comes from model output or repository content, and nothing here widens
authority: budgets, scope, checks and sandbox stay in the frozen intake.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from loopforge.adapters.gemini_supervisor import load_gemini_key
from loopforge.entrypoints.campaign_models import CampaignConfig


@dataclass(frozen=True)
class AgentSettings:
    """Absolute locations, independent of the directory a command is started from."""

    home: Path

    @property
    def state(self) -> Path:
        return self.home / ".loopforge"

    @property
    def key_file(self) -> Path:
        return self.state / "secrets.env"

    @property
    def mlx_lab(self) -> Path:
        return self.state / "mlx-lab"

    @property
    def repository_goals(self) -> Path:
        return self.state / "repository-supervisor"

    @property
    def prepared_goals(self) -> Path:
        return self.state / "supervisor"

    def gemini_key(self, override: Path | None = None) -> str:
        """An explicit key file, else the agent's key file, else ``GEMINI_API_KEY``."""
        if override is not None:
            return load_gemini_key(override)
        return load_gemini_key(self.key_file if self.key_file.is_file() else None)


def agent_settings() -> AgentSettings:
    """``LOOPFORGE_HOME`` when set, otherwise the source checkout this code runs from."""
    configured = os.environ.get("LOOPFORGE_HOME", "").strip()
    home = Path(configured) if configured else Path(__file__).resolve().parents[3]
    if not home.is_absolute():
        msg = "LOOPFORGE_HOME must be an absolute path"
        raise ValueError(msg)
    return AgentSettings(home)


@dataclass(frozen=True)
class WorkerProfile:
    """Every behaviour a coding profile switches, read in one place.

    Model sampling, context window and reasoning come from ``profile_for``; this
    record covers how the harness composes around the model.
    """

    name: str
    # Prompt: continuous-conversation protocol (plan lines, reuse earlier results).
    native_protocol: bool
    template_version: str
    # Tools: pinned Ruff import formatter; read several files in one call follows
    # from the model profile's read window.
    import_formatter: bool
    # Conversation: append-only history without a character cap, measured by bytes.
    history_limit_chars: int | None
    byte_ratio_admission: bool
    # Bookkeeping: only the current failure, not read lists, in each turn.
    conversation_bookkeeping: bool
    # Review: reporting envelope for patch review.
    app_reporting: bool
    bounded_reporting: bool
    large_reporting: bool


WORKER_PROFILES: dict[str, WorkerProfile] = {
    "qwen-native-v1": WorkerProfile(
        name="qwen-native-v1",
        native_protocol=True,
        template_version="1.9.0-native",
        import_formatter=True,
        history_limit_chars=None,
        byte_ratio_admission=True,
        conversation_bookkeeping=True,
        app_reporting=True,
        bounded_reporting=False,
        large_reporting=False,
    ),
    "qwen-bounded-v3": WorkerProfile(
        name="qwen-bounded-v3",
        native_protocol=False,
        template_version="1.1.0-bounded",
        import_formatter=False,
        history_limit_chars=96_000,
        byte_ratio_admission=False,
        conversation_bookkeeping=False,
        app_reporting=False,
        bounded_reporting=True,
        large_reporting=True,
    ),
}


def worker_profile(config: CampaignConfig) -> WorkerProfile:
    return WORKER_PROFILES[config.inference_profile]


@dataclass(frozen=True)
class GateLimits:
    """Code-owned thresholds that stop a worker run, with the values in force today.

    Hard budgets (iterations, tokens, seconds) stay in the intake. These are the
    progress and failure rules around them; step 3 of the WorkBuddy migration
    redesigns them and freezes the result into new intakes.
    """

    # Verifications in a row whose score does not beat the best (capped by max_iterations).
    no_progress_verifications: int = 8
    # Turns in a row with an unchanged patch and no new source read.
    no_new_evidence_turns: int = 8
    # Infrastructure failures of one tool without a success of that tool in between
    # (refused model input does not count).
    tool_circuit_failures: int = 3
    # Local inference is free; the runtime still needs a finite cost ceiling.
    local_cost_usd: float = 1.0
    # Worker evidence larger than this reaches the supervisor as a retrievable artifact.
    supervisor_evidence_bytes: int = 24_000

    def __post_init__(self) -> None:
        counts = (
            self.no_progress_verifications,
            self.no_new_evidence_turns,
            self.tool_circuit_failures,
            self.supervisor_evidence_bytes,
        )
        if any(type(value) is not int or value < 1 for value in counts) or not (
            self.local_cost_usd > 0
        ):
            msg = "Gate limits must be positive"
            raise ValueError(msg)


GATE_LIMITS = GateLimits()
