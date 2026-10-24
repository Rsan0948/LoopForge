"""Agent configuration: absolute locations, explicit profile records, named gate limits."""

from pathlib import Path

import pytest

from loopforge.adapters.memory import InMemoryEventStore
from loopforge.entrypoints import campaign_coding as coding
from loopforge.entrypoints.agent_config import (
    GATE_LIMITS,
    WORKER_PROFILES,
    GateLimits,
    agent_settings,
    worker_profile,
)
from loopforge.entrypoints.campaign_models import CampaignConfig
from tests.unit.test_campaign_coding import FakeChecks, campaign_config, contract, workspace


def test_settings_are_absolute_and_independent_of_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOOPFORGE_HOME", raising=False)
    here = agent_settings()
    monkeypatch.chdir(tmp_path)
    assert agent_settings() == here
    assert here.home.is_absolute()
    assert (here.home / "pyproject.toml").is_file()
    assert here.key_file == here.home / ".loopforge" / "secrets.env"
    assert here.mlx_lab == here.home / ".loopforge" / "mlx-lab"


def test_home_override_must_be_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOPFORGE_HOME", str(tmp_path))
    assert agent_settings().state == tmp_path / ".loopforge"
    monkeypatch.setenv("LOOPFORGE_HOME", "relative/home")
    with pytest.raises(ValueError, match="absolute"):
        agent_settings()


def test_gemini_key_prefers_override_then_agent_file_then_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOPFORGE_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "from-environment")
    settings = agent_settings()
    assert settings.gemini_key() == "from-environment"
    settings.state.mkdir()
    settings.key_file.write_text("GEMINI_API_KEY=from-agent-file\n")
    assert settings.gemini_key() == "from-agent-file"
    override = tmp_path / "other.env"
    override.write_text("GEMINI_API_KEY=from-override\n")
    assert settings.gemini_key(override) == "from-override"


def test_every_supported_profile_has_a_record_and_no_other() -> None:
    supported = set(CampaignConfig.model_fields["inference_profile"].annotation.__args__)  # type: ignore[union-attr]
    assert set(WORKER_PROFILES) == supported
    for name, record in WORKER_PROFILES.items():
        assert record.name == name


@pytest.mark.parametrize("profile", sorted(WORKER_PROFILES))
def test_composition_follows_the_profile_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FakeChecks)
    repo = workspace(tmp_path)
    config = campaign_config(repo).model_copy(update={"inference_profile": profile})
    record = worker_profile(config)
    runtime, live = coding.build_coding(config, contract(), repo, store=InMemoryEventStore())
    assert live is not None
    assert live._template.version == record.template_version  # pyright: ignore[reportPrivateUsage]
    assert live._max_history_chars == record.history_limit_chars  # pyright: ignore[reportPrivateUsage]
    tools = {spec.name for spec in live._tools}  # pyright: ignore[reportPrivateUsage]
    assert ("format_python_imports" in tools) is record.import_formatter
    assert isinstance(runtime.tools, coding.ConfinedCodingTools)
    assert isinstance(runtime.verifier, coding.CodingVerifier)
    assert runtime.verifier.construction_credit is record.construction_credit
    assert runtime.verifier.app_workload is record.app_reporting
    assert runtime.verifier.bounded_reporting is record.bounded_reporting


def test_gate_limits_reach_every_rule_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", FakeChecks)
    repo = workspace(tmp_path)
    gates = GateLimits(
        no_progress_verifications=5,
        no_new_evidence_turns=6,
        tool_circuit_failures=7,
        local_cost_usd=2.0,
    )
    runtime, _ = coding.build_coding(
        campaign_config(repo), contract(), repo, store=InMemoryEventStore(), gates=gates
    )
    assert runtime.control.no_progress_limit == 5
    assert runtime.control.budget.max_cost_usd == 2.0
    assert runtime.reliability.circuit_failure_threshold == 7
    assert isinstance(runtime.model, coding.RequiredChecksAwareModel)
    assert runtime.model.no_new_evidence_turns == 6


def test_default_gate_limits_are_todays_values() -> None:
    assert GateLimits(8, 8, 3, 1.0, 24_000) == GATE_LIMITS


@pytest.mark.parametrize(
    "field", ["no_progress_verifications", "tool_circuit_failures", "supervisor_evidence_bytes"]
)
def test_gate_limits_reject_nonpositive_values(field: str) -> None:
    with pytest.raises(ValueError, match="positive"):
        GateLimits(**{field: 0})  # type: ignore[arg-type]
