"""Policy registry store tests (PACS-017 M6).

The registry is operator-owned state: atomic per-record artifacts, exact-key
envelopes, and domain revalidation on every load — a tampered registry fails
loudly, never silently. Lifecycle transitions go through the domain table, so
promotion without an evidence basis (or without an evidence-gathering state)
is denied at store level too. Allow+deny pairs per AGENTS.md rule 10.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from loopforge.adapters.scripted import FixedClock
from loopforge.domain.policies import (
    ContextAllocationBounds,
    ExecutionPolicy,
    PolicyLifecycle,
    PolicyRoutingKnobs,
)
from loopforge.domain.routing import ModelTier
from loopforge.entrypoints.policy import (
    PolicyRegistryConflictError,
    PolicyRegistryError,
    PolicyRegistryStore,
    UnknownPolicyRecordError,
)

EARLIER = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def _policy(
    policy_id: str = "candidate-x",
    version: int = 1,
    **overrides: object,
) -> ExecutionPolicy:
    base: dict[str, object] = {
        "policy_id": policy_id,
        "version": version,
        "routing": PolicyRoutingKnobs(
            default_tier=ModelTier.ADVANCED,
            stall_escalation_threshold=3,
            budget_pressure_remaining_fraction=0.25,
        ),
        "context_allocation": ContextAllocationBounds(
            floor_tokens=1024,
            ceiling_tokens=4096,
            reserve_tokens=128,
            step_tokens=256,
            low_utilization_fraction=0.4,
        ),
        "verify_read_only_turns": True,
        "worker_count": 2,
    }
    base.update(overrides)
    return ExecutionPolicy(**base)  # pyright: ignore[reportArgumentType]


def _register(
    store: PolicyRegistryStore,
    policy_id: str = "candidate-x",
    version: int = 1,
    **overrides: object,
):
    return store.register(
        _policy(policy_id, version, **overrides),
        evidence_basis=f"registered {policy_id} v{version}",
        note="operator note",
    )


def _rewrite(
    directory: Path,
    policy_id: str,
    version: int,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    path = directory / f"{policy_id}--v{version}.json"
    payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    assert isinstance(payload, dict)
    mutate(payload)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    record = _register(store)

    loaded = store.get("candidate-x", version=1)

    assert loaded == record
    policy = loaded.policy
    assert policy.routing.default_tier is ModelTier.ADVANCED
    assert policy.routing.stall_escalation_threshold == 3
    assert policy.routing.budget_pressure_remaining_fraction == 0.25
    assert policy.context_allocation.floor_tokens == 1024
    assert policy.context_allocation.ceiling_tokens == 4096
    assert policy.context_allocation.reserve_tokens == 128
    assert policy.context_allocation.step_tokens == 256
    assert policy.context_allocation.low_utilization_fraction == 0.4
    assert policy.verify_read_only_turns is True
    assert policy.worker_count == 2
    assert loaded.lifecycle is PolicyLifecycle.CANDIDATE
    assert loaded.evidence_basis == "registered candidate-x v1"
    assert loaded.note == "operator note"


def test_register_writes_one_json_file_atomically(tmp_path: Path) -> None:
    directory = tmp_path / "registry"
    store = PolicyRegistryStore(directory, clock=FixedClock(EARLIER))

    _register(store)

    files = list(directory.iterdir())
    assert [path.name for path in files] == ["candidate-x--v1.json"]
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["updated_at"] == EARLIER.isoformat()


def test_register_a_duplicate_version_fails_closed(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)

    with pytest.raises(PolicyRegistryConflictError, match="already registered"):
        _register(store)

    # The original artifact is untouched by the rejected duplicate.
    assert store.get("candidate-x", version=1).evidence_basis == "registered candidate-x v1"


def test_get_unknown_policy_id_fails_closed(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path)
    _register(store)
    with pytest.raises(UnknownPolicyRecordError, match="unknown registered policy"):
        store.get("no-such-policy")


def test_get_unknown_version_fails_closed(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path)
    _register(store)
    with pytest.raises(UnknownPolicyRecordError, match="unknown version 99"):
        store.get("candidate-x", version=99)


def test_unknown_and_unsafe_ids_never_leak_the_store_path(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path / "secret-registry")
    with pytest.raises(UnknownPolicyRecordError) as unknown:
        store.get("no-such-policy")
    with pytest.raises(UnknownPolicyRecordError) as unsafe:
        # An unsafe id is simply unaddressable — get never builds a path from
        # it, so traversal shapes land in the same unknown-id taxonomy.
        store.get("../../etc/passwd")
    for exc_info in (unknown, unsafe):
        assert "secret-registry" not in str(exc_info.value)
        assert str(tmp_path) not in str(exc_info.value)


def test_register_rejects_an_unsafe_policy_id(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path)
    with pytest.raises(ValueError, match="policy_id"):
        store.register(_policy("a/b"), evidence_basis="registered")


def test_get_without_version_prefers_the_highest(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store, version=1)
    _register(store, version=3)
    _register(store, version=2)

    assert store.get("candidate-x").policy.version == 3


def test_list_is_empty_when_the_directory_is_missing(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path / "never-created")
    assert store.list() == ()
    assert store.promoted() == ()


def test_list_sorts_by_policy_id_then_version(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store, "candidate-b", 2)
    _register(store, "candidate-a", 1)
    _register(store, "candidate-b", 1)

    assert [(r.policy.policy_id, r.policy.version) for r in store.list()] == [
        ("candidate-a", 1),
        ("candidate-b", 1),
        ("candidate-b", 2),
    ]


def test_transition_persists_the_new_lifecycle_and_basis(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)

    updated = store.transition(
        "candidate-x", 1, PolicyLifecycle.SHADOWED, evidence_basis="shadow run run_abc"
    )

    reloaded = PolicyRegistryStore(tmp_path).get("candidate-x", version=1)
    assert updated == reloaded
    assert reloaded.lifecycle is PolicyLifecycle.SHADOWED
    assert reloaded.evidence_basis == "shadow run run_abc"
    assert reloaded.note == "operator note"


def test_transition_to_promoted_from_a_candidate_is_denied(tmp_path: Path) -> None:
    # The store-level pin of "promotion requires evidence-gathering first":
    # CANDIDATE -> PROMOTED is not in the domain transition table.
    store = PolicyRegistryStore(tmp_path)
    _register(store)
    with pytest.raises(ValueError, match="is not legal"):
        store.transition("candidate-x", 1, PolicyLifecycle.PROMOTED, evidence_basis="eval-report-7")
    assert store.get("candidate-x").lifecycle is PolicyLifecycle.CANDIDATE


def test_transition_without_an_evidence_basis_is_denied(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path)
    _register(store)
    with pytest.raises(ValueError, match="evidence_basis cannot be empty"):
        store.transition("candidate-x", 1, PolicyLifecycle.SHADOWED, evidence_basis="  ")


def test_promoted_returns_the_auditable_promotion_history(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store, version=1)
    _register(store, version=2)
    store.transition("candidate-x", 1, PolicyLifecycle.SHADOWED, evidence_basis="run-1")
    store.transition("candidate-x", 1, PolicyLifecycle.PROMOTED, evidence_basis="eval-1")
    store.transition("candidate-x", 2, PolicyLifecycle.BENCHMARKED, evidence_basis="eval-2")
    store.transition("candidate-x", 2, PolicyLifecycle.PROMOTED, evidence_basis="eval-3")

    # PROMOTED is terminal, so supersession is a NEW version — both records
    # stay visible as the promotion history.
    assert [record.policy.version for record in store.promoted()] == [1, 2]


def test_transition_replaces_the_note_only_when_given(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path)
    _register(store)
    carried = store.transition("candidate-x", 1, PolicyLifecycle.SHADOWED, evidence_basis="run-1")
    assert carried.note == "operator note"
    replaced = store.transition(
        "candidate-x",
        1,
        PolicyLifecycle.BENCHMARKED,
        evidence_basis="eval-1",
        note="benchmarked clean",
    )
    assert replaced.note == "benchmarked clean"


def test_save_sweeps_only_its_own_stale_tmp_files(tmp_path: Path) -> None:
    directory = tmp_path / "registry"
    directory.mkdir()
    stale = directory / "candidate-x--v1.json.999.tmp"
    stale.write_text("partial", encoding="utf-8")
    # A foreign .tmp file (another tool's lockfile) is never swept.
    foreign = directory / "orphan.tmp"
    foreign.write_text("not ours", encoding="utf-8")
    store = PolicyRegistryStore(directory, clock=FixedClock(EARLIER))

    _register(store)

    assert not stale.exists()
    assert foreign.exists()


def test_concurrent_saves_across_threads_all_persist(tmp_path: Path) -> None:
    # M9: the REST promote route runs in the FastAPI threadpool, so saves
    # from several threads share the pid — the tmp path defense must cover
    # threads, not just processes.
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))

    def register_one(index: int) -> None:
        _register(store, f"candidate-{index:02d}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register_one, range(40)))

    assert len(store.list()) == 40


def test_misnamed_file_is_tampering_not_a_phantom_record(tmp_path: Path) -> None:
    # M9: filename/content binding — copying a record under another version
    # name must fail loudly, never inject a phantom "latest" version.
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)
    (tmp_path / "candidate-x--v1.json").replace(tmp_path / "candidate-x--v9.json")

    with pytest.raises(PolicyRegistryError, match="holds candidate-x v1"):
        store.list()


def test_unreadable_file_fails_in_the_store_taxonomy(tmp_path: Path) -> None:
    # M9: an unreadable artifact is server-side corruption (500 {detail}),
    # never an escaping OSError bare 500.
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)
    path = tmp_path / "candidate-x--v1.json"
    path.chmod(0o000)
    try:
        with pytest.raises(PolicyRegistryError, match="unreadable") as excinfo:
            store.list()
        assert str(tmp_path) not in str(excinfo.value)
    finally:
        path.chmod(0o644)


@pytest.mark.parametrize("marker", [True, 1.0, "1"])
def test_non_integer_schema_version_fails_closed(tmp_path: Path, marker: object) -> None:
    # M9: the schema marker is type-checked — JSON true/1.0/"1" are not v1.
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)
    _rewrite(tmp_path, "candidate-x", 1, lambda payload: payload.update(schema_version=marker))

    with pytest.raises(PolicyRegistryError, match="schema version"):
        store.list()


def test_non_string_updated_at_fails_closed(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)
    _rewrite(tmp_path, "candidate-x", 1, lambda payload: payload.update(updated_at=12345))

    with pytest.raises(PolicyRegistryError, match="updated_at must be a string"):
        store.list()


def test_corrupt_json_fails_closed_on_list_and_get(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)
    path = tmp_path / "candidate-x--v1.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(PolicyRegistryError, match="not valid JSON"):
        store.list()
    with pytest.raises(PolicyRegistryError, match="not valid JSON"):
        store.get("candidate-x")


def test_wrong_schema_version_fails_closed(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)
    _rewrite(tmp_path, "candidate-x", 1, lambda payload: payload.update(schema_version=99))

    with pytest.raises(PolicyRegistryError, match="schema version 99"):
        store.list()


def _drop_record_note(payload: dict[str, object]) -> None:
    cast("dict[str, object]", payload["record"]).pop("note")


def _add_record_extra_key(payload: dict[str, object]) -> None:
    cast("dict[str, object]", payload["record"]).update(extra=1)


def _drop_policy_worker_count(payload: dict[str, object]) -> None:
    record = cast("dict[str, object]", payload["record"])
    cast("dict[str, object]", record["policy"]).pop("worker_count")


def _drop_envelope_updated_at(payload: dict[str, object]) -> None:
    payload.pop("updated_at")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_drop_record_note, id="record-key-missing"),
        pytest.param(_add_record_extra_key, id="record-key-extra"),
        pytest.param(_drop_policy_worker_count, id="policy-key-missing"),
        pytest.param(_drop_envelope_updated_at, id="envelope-key-missing"),
    ],
)
def test_drifted_keys_fail_closed(
    tmp_path: Path, mutate: Callable[[dict[str, object]], None]
) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)
    _rewrite(tmp_path, "candidate-x", 1, mutate)

    with pytest.raises(PolicyRegistryError, match="keys drifted"):
        store.list()


def test_tampered_knobs_fail_domain_revalidation(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)

    def mutate(payload: dict[str, object]) -> None:
        record = cast("dict[str, object]", payload["record"])
        policy = cast("dict[str, object]", record["policy"])
        policy["version"] = 0

    _rewrite(tmp_path, "candidate-x", 1, mutate)

    with pytest.raises(PolicyRegistryError, match="domain revalidation"):
        store.list()


def test_tampered_lifecycle_fails_domain_revalidation(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)

    def mutate(payload: dict[str, object]) -> None:
        record = cast("dict[str, object]", payload["record"])
        record["lifecycle"] = "enshrined"

    _rewrite(tmp_path, "candidate-x", 1, mutate)

    with pytest.raises(PolicyRegistryError, match="domain revalidation"):
        store.list()


def test_non_object_envelope_fails_closed(tmp_path: Path) -> None:
    store = PolicyRegistryStore(tmp_path, clock=FixedClock(EARLIER))
    _register(store)
    (tmp_path / "candidate-x--v1.json").write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(PolicyRegistryError, match="must be a JSON object"):
        store.list()
