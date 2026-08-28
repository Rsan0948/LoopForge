"""Construction-validated registry of routable model adapters.

Mirrors ``CompositeToolExecutor``: duplicate provider/model registration
fails at construction, capability matching fails closed against code-owned
``ModelRequirements``, and identity lookups are explicit. Registration is
wiring-time authority — entrypoints bind honest per-model metadata (context
windows, cost rates) here rather than trusting adapter defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

from loopforge.domain.routing import ModelCapabilities, ModelRequirements, ModelTier
from loopforge.ports.model import ModelPort


class ModelLookupError(LookupError):
    """Raised when a registry lookup names an unregistered provider/model."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelRegistryEntry:
    """One registered model adapter with its code-owned tier assignment."""

    model: ModelPort
    tier: ModelTier

    def __post_init__(self) -> None:
        # Coerce through the enum so plain-string tiers fail at wiring time.
        object.__setattr__(self, "tier", ModelTier(self.tier))
        # Boundary validation is intentional: adapters may violate port types,
        # and a model lacking the property entirely must fail with the same
        # normalized error rather than a raw AttributeError.
        capabilities = getattr(self.model, "capabilities", None)
        if not isinstance(capabilities, ModelCapabilities):
            msg = "model registry entry capabilities must be ModelCapabilities"
            raise TypeError(msg)

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.model.capabilities


class ModelRegistry:
    """Registry of model adapters keyed by their advertised capabilities."""

    def __init__(self, entries: tuple[ModelRegistryEntry, ...]) -> None:
        if not entries:
            msg = "model registry requires at least one entry"
            raise ValueError(msg)
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            identity = (entry.capabilities.provider, entry.capabilities.model)
            if identity in seen:
                msg_2 = f"model {identity[0]}/{identity[1]} is registered more than once"
                raise ValueError(msg_2)
            seen.add(identity)
        self._entries = entries

    @property
    def entries(self) -> tuple[ModelRegistryEntry, ...]:
        return self._entries

    def candidates(self, requirements: ModelRequirements) -> tuple[ModelRegistryEntry, ...]:
        """Registered models satisfying the code-owned requirements.

        Fail closed: an incompatible model is simply never a candidate, so a
        task requiring an unsupported capability cannot be routed to it.
        """
        return tuple(
            entry for entry in self._entries if not requirements.missing_for(entry.capabilities)
        )

    def entry_for(self, identity: ModelCapabilities) -> ModelRegistryEntry:
        """Look up the entry whose capabilities identify ``identity``."""
        for entry in self._entries:
            capabilities = entry.capabilities
            if (capabilities.provider, capabilities.model) == (identity.provider, identity.model):
                return entry
        msg = f"unregistered model: {identity.provider}/{identity.model}"
        raise ModelLookupError(msg)
