"""Unit tests for the runtime's workload-agnostic artifact evidence seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from loopforge.adapters.context import BasicContextBuilder
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import (
    FixedClock,
    ObservationContainsVerifier,
    RecordingSleeper,
    ScriptedModel,
    ScriptedTools,
)
from loopforge.application.runtime import Runtime
from loopforge.domain.actions import ActionProposal
from loopforge.domain.artifacts import MAX_ARTIFACT_CONTENT_BYTES, ArtifactKind
from loopforge.domain.events import ArtifactRecorded, Event, RunStopped, VerificationPassed
from loopforge.domain.policy import ControlPolicy, PermissionPolicy
from loopforge.domain.reliability import ReliabilityPolicy
from loopforge.domain.state import RunState
from loopforge.domain.tooling import (
    ApprovalClass,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import (
    ActionId,
    BudgetLimit,
    EventId,
    Permission,
    RiskLevel,
    RunId,
    RunStatus,
    StopReason,
)
from loopforge.ports.artifacts import RunArtifact
from loopforge.ports.tools import ToolResult
from loopforge.ports.verifier import VerifierContractError
from loopforge.ports.workspace import WorkspaceError

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


class StubArtifactCollector:
    def __init__(self, artifacts: tuple[object, ...], *, unique_per_call: bool = False) -> None:
        # Deliberately unchecked: contract-violation tests feed non-artifacts through.
        self._artifacts = cast("tuple[RunArtifact, ...]", artifacts)
        self._unique_per_call = unique_per_call
        self.calls = 0

    def collect(self, state: RunState) -> tuple[RunArtifact, ...]:
        del state  # artifact collection is state-independent in this stub
        self.calls += 1
        if not self._unique_per_call:
            return self._artifacts
        # Fresh evidence per cycle: content varies, so the runtime's
        # identical-re-record dedup must not drop it.
        return tuple(
            replace(artifact, content=f"{artifact.content} (cycle {self.calls})")
            for artifact in self._artifacts
        )


def _metadata() -> ToolMetadata:
    return ToolMetadata(
        name="inspect",
        risk=RiskLevel.READ_ONLY,
        required_permission=Permission.READ,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetryClass.SAFE,
        idempotency=IdempotencyClass.NATURAL,
        approval=ApprovalClass.NONE,
        timeout_seconds=5.0,
    )


def _runtime(
    *,
    store: InMemoryEventStore,
    collector: StubArtifactCollector | None,
    results: list[ToolResult] | None = None,
    actions: list[ActionProposal] | None = None,
) -> Runtime:
    return Runtime(
        model=ScriptedModel(actions or [ActionProposal(ActionId("a1"), "inspect", {})]),
        tools=ScriptedTools(
            results or [ToolResult(ok=True, observation="all tests pass")],
            metadata=[_metadata()],
        ),
        verifier=ObservationContainsVerifier("all tests pass"),
        store=store,
        control=ControlPolicy(BudgetLimit(max_cost_usd=1.0, max_iterations=5)),
        permissions=PermissionPolicy(frozenset({Permission.READ})),
        reliability=ReliabilityPolicy(),
        context=BasicContextBuilder(FixedClock(NOW)),
        clock=FixedClock(NOW),
        sleeper=RecordingSleeper(),
        artifacts=collector,
        # Legacy cadence (PACS-016 M8): artifact recording is verification-
        # driven, and these pins use a READ-class tool — preserved under the
        # selectable legacy knob.
        verify_read_only_turns=True,
    )


def _artifact_events(events: tuple[Event, ...]) -> list[ArtifactRecorded]:
    return [event for event in events if isinstance(event, ArtifactRecorded)]


def test_artifacts_are_persisted_after_passing_verification() -> None:
    store = InMemoryEventStore()
    collector = StubArtifactCollector(
        (
            RunArtifact(
                kind=ArtifactKind.WORKSPACE_SNAPSHOT,
                label="workspace:fixture",
                content="exact patch evidence",
            ),
        )
    )
    runtime = _runtime(store=store, collector=collector)

    state = runtime.run("repair the fixture")

    assert state.status is RunStatus.SUCCEEDED
    events = store.events_for(state.run_id)
    artifacts = _artifact_events(events)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.kind is ArtifactKind.WORKSPACE_SNAPSHOT
    assert artifact.label == "workspace:fixture"
    assert artifact.content == "exact patch evidence"
    # The artifact lands between its verification event and the terminal stop,
    # and the whole stream remains legally replayable.
    passed_at = next(
        index for index, event in enumerate(events) if isinstance(event, VerificationPassed)
    )
    stopped_at = next(index for index, event in enumerate(events) if isinstance(event, RunStopped))
    assert passed_at < events.index(artifact) < stopped_at
    assert runtime.state_for(state.run_id) == state


def test_artifacts_are_recorded_after_failed_verification_too() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(
        store=store,
        collector=StubArtifactCollector(
            (
                RunArtifact(
                    kind=ArtifactKind.WORKSPACE_SNAPSHOT,
                    label="workspace:fixture",
                    content="snapshot",
                ),
            ),
            unique_per_call=True,
        ),
        results=[
            ToolResult(ok=True, observation="tests still failing"),
            ToolResult(ok=True, observation="all tests pass"),
        ],
        actions=[
            ActionProposal(ActionId("a1"), "inspect", {}),
            ActionProposal(ActionId("a2"), "inspect", {}),
        ],
    )

    state = runtime.run("repair the fixture")

    assert state.status is RunStatus.SUCCEEDED
    assert len(_artifact_events(store.events_for(state.run_id))) == 2


def test_missing_collector_keeps_streams_artifact_free() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(store=store, collector=None)

    state = runtime.run("repair the fixture")

    assert state.status is RunStatus.SUCCEEDED
    assert _artifact_events(store.events_for(state.run_id)) == []


def test_identical_evidence_is_not_duplicated_across_verifications() -> None:
    store = InMemoryEventStore()
    collector = StubArtifactCollector(
        (
            RunArtifact(
                kind=ArtifactKind.WORKSPACE_SNAPSHOT,
                label="workspace:fixture",
                content="byte-identical snapshot",
            ),
        )
    )
    runtime = _runtime(
        store=store,
        collector=collector,
        results=[
            ToolResult(ok=True, observation="tests still failing"),
            ToolResult(ok=True, observation="all tests pass"),
        ],
        actions=[
            ActionProposal(ActionId("a1"), "inspect", {}),
            ActionProposal(ActionId("a2"), "inspect", {}),
        ],
    )

    state = runtime.run("repair the fixture")

    assert state.status is RunStatus.SUCCEEDED
    assert collector.calls == 2
    # Both verifications collected, but byte-identical evidence is recorded
    # once: the stream carries no duplicate payloads.
    assert len(_artifact_events(store.events_for(state.run_id))) == 1


def test_collector_contract_violations_fail_closed_with_terminal_failure() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(store=store, collector=StubArtifactCollector(("not-an-artifact",)))

    state = runtime.run("repair the fixture")

    # A collector contract violation terminates the run as FAILURE with the
    # reason recorded; it never wedges the run non-terminal, and no partial
    # evidence lands in the stream.
    assert state.status is RunStatus.FAILED
    assert state.stop_reason is StopReason.FAILURE
    stopped = [event for event in store.events_for(state.run_id) if isinstance(event, RunStopped)]
    assert len(stopped) == 1
    assert "ArtifactContractError" in (stopped[0].summary or "")
    assert _artifact_events(store.events_for(state.run_id)) == []


def test_artifact_recorded_rejects_blank_label() -> None:
    with pytest.raises(ValueError, match="artifact label cannot be empty"):
        ArtifactRecorded(
            event_id=EventId("e1"),
            run_id=RunId("run-artifacts"),
            occurred_at=NOW,
            sequence=1,
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="",
            content="x",
        )


def test_terminal_stop_still_records_after_artifacts() -> None:
    store = InMemoryEventStore()
    collector = StubArtifactCollector(
        (
            RunArtifact(
                kind=ArtifactKind.WORKSPACE_SNAPSHOT,
                label="workspace:fixture",
                content="snapshot",
            ),
        )
    )
    runtime = _runtime(store=store, collector=collector)

    state = runtime.run("repair the fixture")

    stopped = [event for event in store.events_for(state.run_id) if isinstance(event, RunStopped)]
    assert stopped[-1].reason is StopReason.SUCCESS_VERIFIED


# --- PACS-010 hardening edge pins ---


class RaisingArtifactCollector:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, state: RunState) -> tuple[RunArtifact, ...]:
        del state
        self.calls += 1
        msg = "workspace snapshot exceeds the artifact byte budget"
        raise WorkspaceError(msg)


def test_collector_failure_terminates_the_run_with_the_reason_recorded() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(store=store, collector=RaisingArtifactCollector())  # pyright: ignore[reportArgumentType]

    state = runtime.run("repair the fixture")

    # Fail closed, never wedged: the run reaches a durable terminal state.
    assert state.status is RunStatus.FAILED
    assert state.stop_reason is StopReason.FAILURE
    stopped = [event for event in store.events_for(state.run_id) if isinstance(event, RunStopped)]
    assert len(stopped) == 1
    assert "artifact collection failed" in (stopped[0].summary or "")
    assert "byte budget" in (stopped[0].summary or "")
    # Resume on the terminally-stopped run is a no-op, not a re-raise loop.
    assert runtime.resume(state.run_id).status is RunStatus.FAILED


class NotAVerificationResultVerifier:
    def verify(self, state: RunState) -> object:
        del state
        return "definitely not a VerificationResult"


def test_verifier_result_contract_is_boundary_checked() -> None:
    store = InMemoryEventStore()
    runtime = _runtime(store=store, collector=None)
    broken = replace(runtime, verifier=NotAVerificationResultVerifier())  # pyright: ignore[reportArgumentType]

    with pytest.raises(VerifierContractError, match="expected VerificationResult"):
        broken.run("repair the fixture")


def test_run_artifact_validation_is_strict() -> None:
    with pytest.raises(TypeError, match="kind must be an ArtifactKind"):
        RunArtifact(kind="workspace_snapshot", label="a", content="c")  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="control characters"):
        RunArtifact(kind=ArtifactKind.WORKSPACE_SNAPSHOT, label="a\nb", content="c")
    with pytest.raises(ValueError, match="byte budget"):
        RunArtifact(
            kind=ArtifactKind.WORKSPACE_SNAPSHOT,
            label="a",
            content="x" * (MAX_ARTIFACT_CONTENT_BYTES + 1),
        )
