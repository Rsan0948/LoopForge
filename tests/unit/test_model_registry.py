"""Unit tests for the model capability registry and routing vocabulary."""

from __future__ import annotations

import pytest

from loopforge.adapters.model_registry import (
    ModelLookupError,
    ModelRegistry,
    ModelRegistryEntry,
)
from loopforge.adapters.scripted import ScriptedModel
from loopforge.domain.actions import ActionProposal
from loopforge.domain.routing import (
    ModelCapabilities,
    ModelRequirements,
    ModelTier,
    RoutingPolicyConfig,
)
from loopforge.domain.types import ActionId


def _model(  # noqa: PLR0913 - capability knobs stay explicit at each call site
    provider: str = "stub",
    name: str = "fake",
    *,
    tool_calls: bool = True,
    window: int = 8192,
    input_cost: float = 0.0,
    output_cost: float = 0.0,
) -> ScriptedModel:
    return ScriptedModel(
        [ActionProposal(ActionId("a1"), "inspect", {})],
        capabilities=ModelCapabilities(
            provider=provider,
            model=name,
            supports_tool_calls=tool_calls,
            context_window_tokens=window,
            input_cost_usd_per_million=input_cost,
            output_cost_usd_per_million=output_cost,
        ),
    )


def _entry(model: ScriptedModel, tier: ModelTier = ModelTier.STANDARD) -> ModelRegistryEntry:
    return ModelRegistryEntry(model=model, tier=tier)


# --- ScriptedModel honest capabilities -------------------------------------------


def test_scripted_model_advertises_honest_default_capabilities() -> None:
    model = ScriptedModel([ActionProposal(ActionId("a1"), "inspect", {})])
    capabilities = model.capabilities
    assert capabilities.provider == "scripted"
    assert capabilities.supports_tool_calls is True
    assert capabilities.context_window_tokens == 4096
    assert capabilities.input_cost_usd_per_million == 0.0
    assert capabilities.output_cost_usd_per_million == 0.0


def test_scripted_model_accepts_wiring_supplied_capabilities() -> None:
    model = _model(provider="acme", name="big", window=200_000, input_cost=3.0)
    assert model.capabilities.provider == "acme"
    assert model.capabilities.context_window_tokens == 200_000


# --- ModelRequirements matching ---------------------------------------------------


def test_empty_requirements_match_every_model() -> None:
    assert ModelRequirements().missing_for(_model().capabilities) == ()


def test_tool_call_requirement_fails_closed() -> None:
    requirements = ModelRequirements(supports_tool_calls=True)
    assert requirements.missing_for(_model(tool_calls=False).capabilities) == (
        "supports_tool_calls",
    )
    assert requirements.missing_for(_model(tool_calls=True).capabilities) == ()


def test_context_window_requirement_passes_at_exact_boundary() -> None:
    requirements = ModelRequirements(min_context_window_tokens=8192)
    assert requirements.missing_for(_model(window=8192).capabilities) == ()
    assert requirements.missing_for(_model(window=8191).capabilities) == (
        "min_context_window_tokens",
    )


def test_cost_ceiling_requirements_fail_closed() -> None:
    requirements = ModelRequirements(
        max_input_cost_usd_per_million=1.0,
        max_output_cost_usd_per_million=2.0,
    )
    cheap = _model(input_cost=1.0, output_cost=2.0).capabilities
    pricey = _model(input_cost=1.5, output_cost=2.5).capabilities
    assert requirements.missing_for(cheap) == ()
    assert requirements.missing_for(pricey) == (
        "max_input_cost_usd_per_million",
        "max_output_cost_usd_per_million",
    )


def test_requirements_validation_rejects_bad_windows() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        ModelRequirements(min_context_window_tokens=True)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="cannot be negative"):
        ModelRequirements(min_context_window_tokens=-1)


def test_requirements_validation_rejects_bad_cost_ceilings() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        ModelRequirements(max_input_cost_usd_per_million=float("nan"))
    with pytest.raises(ValueError, match="must be finite"):
        ModelRequirements(max_output_cost_usd_per_million=float("inf"))
    with pytest.raises(ValueError, match="cannot be negative"):
        ModelRequirements(max_input_cost_usd_per_million=-0.5)


# --- RoutingPolicyConfig validation ------------------------------------------------


def test_routing_config_defaults_are_deterministic() -> None:
    config = RoutingPolicyConfig(requirements=ModelRequirements())
    assert config.default_tier is ModelTier.STANDARD
    assert config.stall_escalation_threshold == 2
    assert config.budget_pressure_remaining_fraction is None


def test_routing_config_validation() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        RoutingPolicyConfig(
            requirements=ModelRequirements(),
            stall_escalation_threshold=True,  # pyright: ignore[reportArgumentType]
        )
    with pytest.raises(ValueError, match="must be positive"):
        RoutingPolicyConfig(requirements=ModelRequirements(), stall_escalation_threshold=0)
    with pytest.raises(ValueError, match="must be finite"):
        RoutingPolicyConfig(
            requirements=ModelRequirements(),
            budget_pressure_remaining_fraction=float("nan"),
        )
    with pytest.raises(ValueError, match=r"\(0, 1\]"):
        RoutingPolicyConfig(
            requirements=ModelRequirements(), budget_pressure_remaining_fraction=0.0
        )
    with pytest.raises(ValueError, match=r"\(0, 1\]"):
        RoutingPolicyConfig(
            requirements=ModelRequirements(), budget_pressure_remaining_fraction=1.5
        )


# --- Registry construction ----------------------------------------------------------


def test_registry_requires_at_least_one_entry() -> None:
    with pytest.raises(ValueError, match="at least one entry"):
        ModelRegistry(())


def test_duplicate_provider_model_registration_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="registered more than once"):
        ModelRegistry((_entry(_model(name="dup")), _entry(_model(name="dup"))))


def test_same_model_name_under_different_providers_is_allowed() -> None:
    registry = ModelRegistry(
        (_entry(_model(provider="a", name="dup")), _entry(_model(provider="b", name="dup")))
    )
    assert len(registry.entries) == 2


def test_entry_coerces_string_tiers_and_rejects_invalid_capability_types() -> None:
    entry = ModelRegistryEntry(
        model=_model(),
        tier="advanced",  # pyright: ignore[reportArgumentType]
    )
    assert entry.tier is ModelTier.ADVANCED

    class _LyingModel:
        @property
        def capabilities(self) -> str:
            return "not capabilities"

    with pytest.raises(TypeError, match="must be ModelCapabilities"):
        ModelRegistryEntry(
            model=_LyingModel(),  # pyright: ignore[reportArgumentType]
            tier=ModelTier.ECONOMY,
        )


# --- Registry lookup and capability matching ----------------------------------------


def test_candidates_fail_closed_on_requirements() -> None:
    registry = ModelRegistry(
        (
            _entry(_model(name="no-tools", tool_calls=False)),
            _entry(_model(name="small", window=2048)),
            _entry(_model(name="fit", window=8192)),
        )
    )
    candidates = registry.candidates(
        ModelRequirements(supports_tool_calls=True, min_context_window_tokens=4096)
    )
    assert [entry.capabilities.model for entry in candidates] == ["fit"]


def test_candidates_empty_when_nothing_is_compatible() -> None:
    registry = ModelRegistry((_entry(_model(tool_calls=False)),))
    assert registry.candidates(ModelRequirements(supports_tool_calls=True)) == ()


def test_entry_for_resolves_identity_and_rejects_unknown_models() -> None:
    registry = ModelRegistry((_entry(_model(provider="a", name="x")),))
    found = registry.entry_for(_model(provider="a", name="x").capabilities)
    assert found.capabilities.provider == "a"
    with pytest.raises(ModelLookupError, match="unregistered model: a/y"):
        registry.entry_for(_model(provider="a", name="y").capabilities)


# --- ModelCapabilities validation -------------------------------------------------


def test_capabilities_validation_rejects_blank_identities() -> None:
    with pytest.raises(ValueError, match="provider cannot be empty"):
        ModelCapabilities(
            provider="  ", model="m", supports_tool_calls=True, context_window_tokens=1
        )
    with pytest.raises(ValueError, match="model cannot be empty"):
        ModelCapabilities(provider="p", model="", supports_tool_calls=True, context_window_tokens=1)


def test_capabilities_validation_rejects_bad_windows_and_rates() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        ModelCapabilities(
            provider="p",
            model="m",
            supports_tool_calls=True,
            context_window_tokens=True,  # pyright: ignore[reportArgumentType]
        )
    with pytest.raises(ValueError, match="must be positive"):
        ModelCapabilities(
            provider="p", model="m", supports_tool_calls=True, context_window_tokens=0
        )
    with pytest.raises(ValueError, match="cannot be negative"):
        ModelCapabilities(
            provider="p",
            model="m",
            supports_tool_calls=True,
            context_window_tokens=1,
            output_cost_usd_per_million=-1.0,
        )
