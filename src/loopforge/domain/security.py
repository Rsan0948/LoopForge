from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum


class TrustClass(StrEnum):
    """Origin/authority class for information that may later enter agent context."""

    RUNTIME_POLICY = "runtime_policy"
    AUTHORIZED_HUMAN = "authorized_human"
    DETERMINISTIC_OBSERVATION = "deterministic_observation"
    EXTERNAL_EVIDENCE = "external_evidence"
    MODEL_INFERENCE = "model_inference"
    UNTRUSTED_CONTENT = "untrusted_content"


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxRequirements:
    """Minimum isolation properties a workload requires before it may execute."""

    file_api_confined: bool = False
    symlink_protected: bool = False
    environment_filtered: bool = False
    process_timeout: bool = False
    resource_limits: bool = False
    output_limited: bool = False
    process_filesystem_isolated: bool = False
    network_isolated: bool = False
    kernel_isolated: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxCapabilities:
    """Security properties an adapter can truthfully enforce."""

    file_api_confined: bool
    symlink_protected: bool
    environment_filtered: bool
    process_timeout: bool
    resource_limits: bool
    output_limited: bool
    process_filesystem_isolated: bool
    network_isolated: bool
    kernel_isolated: bool

    def require(self, requirements: SandboxRequirements | None = None, **legacy: bool) -> None:
        """Fail closed if this sandbox cannot satisfy a workload's declared requirements.

        Keyword requirements remain accepted for the initial PACS-005 API, while callers should
        prefer the explicit SandboxRequirements value for code-owned workload contracts.
        """
        if requirements is not None and legacy:
            raise ValueError("pass either SandboxRequirements or keyword requirements, not both")
        required = requirements or SandboxRequirements(**legacy)
        missing = [
            field.name
            for field in fields(required)
            if getattr(required, field.name) and not getattr(self, field.name)
        ]
        if missing:
            raise ValueError(
                "sandbox does not satisfy required capabilities: " + ", ".join(missing)
            )
