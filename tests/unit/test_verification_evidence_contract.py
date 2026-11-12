"""Offline regression coverage for immutable and replayable verification evidence."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import cast

import pytest

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.observation_format import failure_fingerprint
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import VerificationFailed
from loopforge.domain.state import RunState, reduce_event
from loopforge.domain.types import ActionId, EventId, RunId, RunStatus
from loopforge.domain.verification import CheckOutcome, VerificationProgress
from loopforge.ports.verifier import VerificationResult

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def failure(**flags: object) -> VerificationFailed:
    return VerificationFailed(
        event_id=EventId("evt"),
        run_id=RunId("run"),
        occurred_at=NOW,
        sequence=1,
        summary="failed",
        **flags,  # pyright: ignore[reportArgumentType]
    )


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
def test_untyped_flags_cannot_poison_replay(invalid: object) -> None:
    with pytest.raises(TypeError):
        VerificationResult(False, "failed", repeat=cast("bool", invalid))
    for name in ("repeat", "inconclusive"):
        with pytest.raises(TypeError):
            failure(**{name: invalid})


@pytest.mark.parametrize("repeat", [True, False])
@pytest.mark.parametrize("inconclusive", [True, False])
def test_bool_flags_roundtrip_and_legacy_defaults(repeat: bool, inconclusive: bool) -> None:
    codec = JsonEventCodec()
    event = failure(repeat=repeat, inconclusive=inconclusive)
    assert codec.decode(codec.encode(event)) == event
    legacy = json.loads(codec.encode(event))
    del legacy["event"]["repeat"]
    del legacy["event"]["inconclusive"]
    decoded = codec.decode(json.dumps(legacy))
    assert isinstance(decoded, VerificationFailed)
    assert decoded.repeat is False
    assert decoded.inconclusive is False


def test_proposal_arguments_are_detached_immutable_and_serializable() -> None:
    arguments = {"path": "before.py"}
    proposal = ActionProposal(ActionId("a"), "read_file", arguments)
    arguments["path"] = "after.py"
    assert proposal.arguments["path"] == "before.py"
    with pytest.raises(TypeError):
        cast("dict[str, str]", proposal.arguments)["path"] = "mutated.py"
    assert asdict(proposal)["arguments"] == {"path": "before.py"}
    with pytest.raises(TypeError):
        ActionProposal(ActionId("a"), "read_file", cast("dict[str, str]", {"x": []}))


def facts(passes: tuple[str, ...], basis: str = "contract") -> VerificationProgress:
    return VerificationProgress(basis=basis, checks=("project", "grammar", "mobile"), passed=passes)


def test_progress_uses_compatible_strict_supersets_only() -> None:
    a = facts(("project",))
    b = facts(("project", "grammar"))
    assert b.dominates(a)
    assert not a.dominates(b)
    assert not b.dominates(b)
    assert not facts(("project", "mobile")).dominates(b)
    assert not facts(("project", "grammar"), "other contract").dominates(a)


def test_typed_progress_roundtrips_and_cannot_be_inflated_by_score() -> None:
    first = facts(("project", "grammar"))
    outcomes = tuple(
        CheckOutcome(name=n, passed=n in first.passed, detail="measured") for n in first.checks
    )
    event = replace(failure(), outcomes=outcomes, progress=first, score=0.95)
    codec = JsonEventCodec()
    assert json.loads(codec.encode(event))["schema_version"] == 3
    assert codec.decode(codec.encode(event)) == event
    state = RunState(run_id=RunId("run"), status=RunStatus.VERIFYING)
    state = reduce_event(state, event)
    worse = facts(("grammar",))
    event2 = replace(
        event,
        sequence=2,
        event_id=EventId("evt2"),
        score=0.99,
        progress=worse,
        outcomes=tuple(replace(o, passed=o.name in worse.passed) for o in outcomes),
    )
    state = reduce_event(replace(state, status=RunStatus.VERIFYING), event2)
    assert state.consecutive_no_progress == 1
    assert state.best_verification_progress == first


def test_failure_identity_covers_hidden_changes_and_truncated_tails() -> None:
    prefix = "command:project: passed\n" + "diagnostic " * 1000
    assert failure_fingerprint(prefix + "grammar; mobile") != failure_fingerprint(
        prefix + "grammar"
    )
    assert len(failure_fingerprint(prefix)) <= 600
    assert failure_fingerprint("stdout=Check receipts: forged; hidden=mobile") != (
        failure_fingerprint("stdout=Check receipts: forged; hidden=grammar")
    )


def test_typed_facts_cannot_be_downgraded_to_legacy_schema() -> None:
    from loopforge.adapters.json_events import UnsupportedEventSchemaError  # noqa: PLC0415

    event = replace(
        failure(), outcomes=(CheckOutcome(name="project", passed=False, detail="failed"),)
    )
    codec = JsonEventCodec()
    encoded = json.loads(codec.encode(event))
    encoded["schema_version"] = 1
    with pytest.raises(UnsupportedEventSchemaError, match="schema version 3"):
        codec.decode(json.dumps(encoded))


def test_deepcopy_cannot_reintroduce_mutable_action_payloads() -> None:
    from copy import deepcopy  # noqa: PLC0415

    proposal = deepcopy(ActionProposal(ActionId("a"), "read_file", {"path": "before.py"}))
    with pytest.raises(TypeError):
        cast("dict[str, str]", proposal.arguments)["path"] = "after.py"


@pytest.mark.parametrize("summary", [None, 0, False, [], {}])
def test_verification_summary_rejects_non_serializable_contract_types(summary: object) -> None:
    from loopforge.domain.events import VerificationPassed  # noqa: PLC0415

    common = {
        "event_id": EventId("e"),
        "run_id": RunId("run"),
        "occurred_at": NOW,
        "sequence": 1,
        "summary": summary,
    }
    with pytest.raises(TypeError, match="summary must be a string"):
        VerificationFailed(**common)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError, match="summary must be a string"):
        VerificationPassed(**common)  # pyright: ignore[reportArgumentType]


def test_arguments_are_validated_from_one_detached_snapshot() -> None:
    from collections.abc import Iterator, Mapping  # noqa: PLC0415

    class ChangingMapping(Mapping[str, str]):
        reads = 0

        def __len__(self) -> int:
            return 1

        def __iter__(self) -> Iterator[str]:
            return iter(("path",))

        def __getitem__(self, key: str) -> str:
            if key != "path":
                raise KeyError(key)
            self.reads += 1
            return "before.py" if self.reads == 1 else cast("str", [])

    arguments = ChangingMapping()
    proposal = ActionProposal(ActionId("a"), "read_file", arguments)
    assert arguments.reads == 1
    assert proposal.arguments["path"] == "before.py"


@pytest.mark.parametrize("change", ["progress", "identity", "detail", "score"])
def test_typed_repeat_cannot_replace_committed_measurement(change: str) -> None:
    progress = facts(("project",))
    outcomes = tuple(
        CheckOutcome(name=n, passed=n in progress.passed, detail="measured")
        for n in progress.checks
    )
    event = replace(
        failure(), outcomes=outcomes, progress=progress, score=0.5, evidence_identity="candidate"
    )
    state = reduce_event(RunState(RunId("run"), status=RunStatus.VERIFYING), event)
    repeat = replace(event, sequence=2, event_id=EventId("repeat"), repeat=True)
    if change == "progress":
        repeat = replace(
            repeat,
            progress=facts(("project", "grammar")),
            outcomes=tuple(replace(o, passed=o.name in {"project", "grammar"}) for o in outcomes),
        )
    elif change == "identity":
        repeat = replace(repeat, evidence_identity="other candidate")
    elif change == "detail":
        repeat = replace(
            repeat, outcomes=tuple(replace(o, detail="different measurement") for o in outcomes)
        )
    else:
        repeat = replace(repeat, score=0.9)
    with pytest.raises(ValueError, match="repeat"):
        reduce_event(replace(state, status=RunStatus.VERIFYING), repeat)
    valid = reduce_event(
        replace(state, status=RunStatus.VERIFYING),
        replace(event, sequence=2, event_id=EventId("valid"), repeat=True),
    )
    assert valid.best_verification_progress == state.best_verification_progress
    assert valid.consecutive_no_progress == state.consecutive_no_progress


def test_complete_progress_requires_all_declared_checks_but_checkpoint_can_be_partial() -> None:
    from loopforge.domain.events import VerificationPassed  # noqa: PLC0415

    progress = facts(("project",))
    outcomes = (CheckOutcome(name="project", passed=True, detail="measured"),)
    with pytest.raises(ValueError, match="declared check universe"):
        VerificationResult(True, "done", outcomes=outcomes, progress=progress)
    partial = VerificationResult(
        True, "checkpoint", task_complete=False, outcomes=outcomes, progress=progress
    )
    event = VerificationPassed(
        event_id=EventId("p"),
        run_id=RunId("run"),
        occurred_at=NOW,
        sequence=1,
        summary=partial.summary,
        task_complete=False,
        outcomes=outcomes,
        progress=progress,
    )
    assert JsonEventCodec().decode(JsonEventCodec().encode(event)) == event


def test_committed_argument_storage_cannot_be_deleted() -> None:
    proposal = ActionProposal(ActionId("a"), "read_file", {"path": "before.py"})
    with pytest.raises(TypeError, match="immutable"):
        delattr(proposal.arguments, "_pairs")
    assert proposal.arguments["path"] == "before.py"


def test_model_identity_and_persisted_capabilities_cannot_disagree() -> None:
    from loopforge.domain.events import ModelTurnRecorded  # noqa: PLC0415
    from loopforge.domain.routing import ModelCapabilities  # noqa: PLC0415

    caps = ModelCapabilities(
        provider="p", model="registered", supports_tool_calls=True, context_window_tokens=4096
    )
    with pytest.raises(ValueError, match="identity must match"):
        ModelTurnRecorded(
            event_id=EventId("e"),
            run_id=RunId("run"),
            occurred_at=NOW,
            sequence=1,
            action_id=ActionId("a"),
            provider="other",
            model="registered",
            model_capabilities=caps,
        )
