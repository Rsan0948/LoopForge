"""Operator-owned policy registry (PACS-017 M6).

The controlled promotion workflow: candidate ``ExecutionPolicy`` versions
are registered with an evidence basis, gather shadow/benchmark evidence
through the closed lifecycle, and are promoted only by explicit operator
action (CLI or REST) carrying a literal confirmation and a referenced
evidence basis. Nothing in the runtime performs lifecycle transitions —
a candidate can never self-promote.

Store discipline mirrors ``EvalReportStore`` (ADR-0012 ownership split):
versioned per-record JSON artifacts, atomic tmp-file + rename writes,
exact-key envelopes, and domain revalidation on every load — a tampered
registry fails loudly, never silently. The serving plane reads through
the same ``policy_record_to_dict`` projection so the wire shape is
byte-identical to the operator-owned artifact.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Final, cast

from loopforge.adapters.system_time import SystemClock
from loopforge.domain.policies import (
    ContextAllocationBounds,
    ExecutionPolicy,
    PolicyLifecycle,
    PolicyRecord,
    PolicyRoutingKnobs,
    transition_policy_record,
)
from loopforge.domain.routing import ModelTier
from loopforge.ports.clock import ClockPort

REGISTRY_SCHEMA_VERSION: Final = 1

_ENVELOPE_KEYS: Final = frozenset({"schema_version", "updated_at", "record"})
_RECORD_KEYS: Final = frozenset({"policy", "lifecycle", "evidence_basis", "note"})
_POLICY_KEYS: Final = frozenset(
    {
        "policy_id",
        "version",
        "routing",
        "context_allocation",
        "verify_read_only_turns",
        "worker_count",
    }
)
_ROUTING_KEYS: Final = frozenset(
    {"default_tier", "stall_escalation_threshold", "budget_pressure_remaining_fraction"}
)
_ALLOCATION_KEYS: Final = frozenset(
    {
        "floor_tokens",
        "ceiling_tokens",
        "reserve_tokens",
        "step_tokens",
        "low_utilization_fraction",
    }
)


class PolicyRegistryError(RuntimeError):
    """Registry corruption or I/O failure — always loud, never silent."""


class UnknownPolicyRecordError(PolicyRegistryError):
    """The addressed record does not exist (the 404 family)."""


class PolicyRegistryConflictError(PolicyRegistryError):
    """The operation conflicts with existing registry state (the 409 family)."""


def _require_keys(data: object, expected: frozenset[str], *, what: str) -> dict[str, object]:
    if not isinstance(data, dict):
        msg = f"{what} must be a JSON object"
        raise PolicyRegistryError(msg)
    mapping = cast("dict[str, object]", data)
    keys = frozenset(mapping)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        msg_2 = f"{what} keys drifted (missing: {missing}, extra: {extra})"
        raise PolicyRegistryError(msg_2)
    return mapping


def policy_record_to_dict(record: PolicyRecord) -> dict[str, object]:
    """JSON-safe serialization of one registry record.

    Shared by the atomic writer, the CLI, and the operator server so the
    wire projection of a record is byte-identical to its artifact.
    """
    policy = record.policy
    return {
        "policy": {
            "policy_id": policy.policy_id,
            "version": policy.version,
            "routing": {
                "default_tier": policy.routing.default_tier.value,
                "stall_escalation_threshold": policy.routing.stall_escalation_threshold,
                "budget_pressure_remaining_fraction": (
                    policy.routing.budget_pressure_remaining_fraction
                ),
            },
            "context_allocation": {
                "floor_tokens": policy.context_allocation.floor_tokens,
                "ceiling_tokens": policy.context_allocation.ceiling_tokens,
                "reserve_tokens": policy.context_allocation.reserve_tokens,
                "step_tokens": policy.context_allocation.step_tokens,
                "low_utilization_fraction": policy.context_allocation.low_utilization_fraction,
            },
            "verify_read_only_turns": policy.verify_read_only_turns,
            "worker_count": policy.worker_count,
        },
        "lifecycle": record.lifecycle.value,
        "evidence_basis": record.evidence_basis,
        "note": record.note,
    }


def _policy_record_from_dict(data: object) -> PolicyRecord:
    """Rebuild a record through the domain constructors; tampering fails loudly."""
    fields = _require_keys(data, _RECORD_KEYS, what="policy record")
    policy_fields = _require_keys(fields["policy"], _POLICY_KEYS, what="policy")
    routing_fields = _require_keys(policy_fields["routing"], _ROUTING_KEYS, what="routing")
    allocation_fields = _require_keys(
        policy_fields["context_allocation"], _ALLOCATION_KEYS, what="context allocation"
    )
    try:
        policy = ExecutionPolicy(
            policy_id=policy_fields["policy_id"],  # type: ignore[arg-type]
            version=policy_fields["version"],  # type: ignore[arg-type]
            routing=PolicyRoutingKnobs(
                default_tier=ModelTier(routing_fields["default_tier"]),  # type: ignore[arg-type]
                stall_escalation_threshold=routing_fields["stall_escalation_threshold"],  # type: ignore[arg-type]
                budget_pressure_remaining_fraction=routing_fields[  # type: ignore[arg-type]
                    "budget_pressure_remaining_fraction"
                ],
            ),
            context_allocation=ContextAllocationBounds(
                floor_tokens=allocation_fields["floor_tokens"],  # type: ignore[arg-type]
                ceiling_tokens=allocation_fields["ceiling_tokens"],  # type: ignore[arg-type]
                reserve_tokens=allocation_fields["reserve_tokens"],  # type: ignore[arg-type]
                step_tokens=allocation_fields["step_tokens"],  # type: ignore[arg-type]
                low_utilization_fraction=allocation_fields["low_utilization_fraction"],  # type: ignore[arg-type]
            ),
            verify_read_only_turns=policy_fields["verify_read_only_turns"],  # type: ignore[arg-type]
            worker_count=policy_fields["worker_count"],  # type: ignore[arg-type]
        )
        return PolicyRecord(
            policy=policy,
            lifecycle=PolicyLifecycle(fields["lifecycle"]),  # type: ignore[arg-type]
            evidence_basis=fields["evidence_basis"],  # type: ignore[arg-type]
            note=fields["note"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        msg = f"policy record fails domain revalidation: {exc}"
        raise PolicyRegistryError(msg) from exc


class PolicyRegistryStore:
    """Operator-owned per-record JSON registry with atomic writes.

    Writers (CLI register/promote/retire, REST promote) go through the
    domain transition rules; readers (CLI list/show, REST GET) get
    domain-revalidated records only.
    """

    def __init__(self, directory: str | Path, *, clock: ClockPort | None = None) -> None:
        self._directory = Path(directory)
        self._clock = clock if clock is not None else SystemClock()

    def _path_for(self, policy_id: str, version: int) -> Path:
        return self._directory / f"{policy_id}--v{version}.json"

    def _sweep_stale_tmp_files(self) -> None:
        for stale in self._directory.glob("*.tmp"):
            with suppress(OSError):
                stale.unlink()

    def _save(self, record: PolicyRecord) -> Path:
        payload = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "updated_at": self._clock.now().isoformat(),
            "record": policy_record_to_dict(record),
        }
        path = self._path_for(record.policy.policy_id, record.policy.version)
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._sweep_stale_tmp_files()
            tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(path)
        except OSError as exc:
            msg = f"cannot persist policy record: {exc}"
            raise PolicyRegistryError(msg) from exc
        return path

    def _decode(self, path: Path) -> PolicyRecord:
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = f"policy registry file is not valid JSON: {path.name} ({exc})"
            raise PolicyRegistryError(msg) from exc
        envelope = _require_keys(raw, _ENVELOPE_KEYS, what="policy registry file")
        if envelope["schema_version"] != REGISTRY_SCHEMA_VERSION:
            msg_2 = (
                f"policy registry schema version {envelope['schema_version']!r} is not "
                f"supported (expected {REGISTRY_SCHEMA_VERSION}): {path.name}"
            )
            raise PolicyRegistryError(msg_2)
        return _policy_record_from_dict(envelope["record"])

    def register(
        self,
        policy: ExecutionPolicy,
        *,
        evidence_basis: str,
        note: str = "",
    ) -> PolicyRecord:
        """Register a candidate; a duplicate (id, version) fails closed."""
        if self._path_for(policy.policy_id, policy.version).exists():
            msg = f"policy {policy.policy_id!r} version {policy.version} is already registered"
            raise PolicyRegistryConflictError(msg)
        record = PolicyRecord(
            policy=policy,
            lifecycle=PolicyLifecycle.CANDIDATE,
            evidence_basis=evidence_basis,
            note=note,
        )
        self._save(record)
        return record

    def get(self, policy_id: str, *, version: int | None = None) -> PolicyRecord:
        """Load one record (latest version when unspecified); unknown → 404 family."""
        records = [record for record in self.list() if record.policy.policy_id == policy_id]
        if not records:
            msg = f"unknown registered policy {policy_id!r}"
            raise UnknownPolicyRecordError(msg)
        if version is None:
            return max(records, key=lambda record: record.policy.version)
        for record in records:
            if record.policy.version == version:
                return record
        msg_2 = f"unknown version {version} for registered policy {policy_id!r}"
        raise UnknownPolicyRecordError(msg_2)

    def list(self) -> tuple[PolicyRecord, ...]:
        """Every record, domain-revalidated; a corrupt file fails the whole list."""
        if not self._directory.exists():
            return ()
        records = [self._decode(path) for path in sorted(self._directory.glob("*.json"))]
        return tuple(
            sorted(records, key=lambda record: (record.policy.policy_id, record.policy.version))
        )

    def transition(
        self,
        policy_id: str,
        version: int,
        to: PolicyLifecycle,
        *,
        evidence_basis: str,
        note: str | None = None,
    ) -> PolicyRecord:
        """Move a record along the closed transition table and persist it.

        PROMOTED is terminal, so supersession is registering and promoting
        a NEW version — an auditable promotion history, never a silent
        mutation of the current record.
        """
        record = self.get(policy_id, version=version)
        updated = transition_policy_record(record, to, evidence_basis=evidence_basis, note=note)
        self._save(updated)
        return updated

    def promoted(self) -> tuple[PolicyRecord, ...]:
        """Every PROMOTED record (the auditable promotion history)."""
        return tuple(
            record for record in self.list() if record.lifecycle is PolicyLifecycle.PROMOTED
        )
