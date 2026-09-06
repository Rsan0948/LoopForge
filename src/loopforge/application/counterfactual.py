"""Counterfactual replay (PACS-017 M4).

Given the durable event stream of a historical run, re-drive the run from
a prefix of that stream on a fresh prefix-seeded store through the public
``Runtime.resume`` seam (heal-once + blocking drive), then compare the
re-driven suffix against the historical suffix. The outcome vocabulary is
closed and honest:

* ``MATCHED`` — the re-driven suffix is canonically identical to the
  historical suffix (determinism confirmed for the wired dependencies);
* ``DIVERGED`` — the re-drive settled cleanly and the suffixes differ
  (the counterfactual finding, e.g. a candidate policy changed behavior,
  or the re-drive reached an approval gate it cannot pass on its own);
* ``DIVERGED_UNKNOWN`` — the counterfactual outcome cannot be determined:
  the historical stream is valid but the re-drive cannot proceed
  deterministically (a non-idempotent in-flight side effect would have to
  be re-executed, or the recorded turns ran out). Uncertainty is
  reported, never smoothed over.

Why ``resume`` and not the PACS-013 ``step`` seam: ``step`` re-applies
the resume heal rules on *every* call, so a cycle that ends right after a
recorded verification is re-resolved — and re-verified — on the next
step, while the blocking drive verifies exactly once. ``resume`` heals
exactly once on entry, which is the semantics a prefix re-drive needs.
(The stepped-drive double verification is recorded as an adversarial
review finding for PACS-017 M9.) Termination of the re-drive is
guaranteed by the wired ``ControlPolicy`` budget, the same fail-closed
bound that terminates any driven run.

Corrupted streams fail closed: the full historical stream is validated
through the domain reducer before anything is re-driven, and any
violation raises ``CounterfactualPrefixError``.

Determinism is a property of the *wired* runtime dependencies, so this
module never wires adapters itself (layer rules): the caller provides a
``Runtime`` bound to a fresh store. ``extract_scripted_turns``
reconstructs the recorded model proposals, tool results, tool metadata,
and verification outcomes so a scripted re-drive reproduces the recorded
decisions exactly while the knobs under study (policy routing, context
budgets, verification cadence) are free to differ.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum

from loopforge.application.runtime import Runtime, UnsafeResumeStateError
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import (
    ActionAuthorized,
    ActionProposed,
    Event,
    ShadowDecisionRecorded,
    ToolFailed,
    ToolSucceeded,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.state import InvalidTransitionError, replay
from loopforge.domain.tooling import ToolMetadata
from loopforge.domain.types import RunId, RunStatus
from loopforge.ports.tools import ToolResult
from loopforge.ports.verifier import VerificationResult

_VOLATILE_FIELDS = frozenset({"event_id", "run_id", "occurred_at", "sequence", "caused_by"})


class ReplayOutcome(StrEnum):
    """Closed vocabulary of counterfactual re-drive outcomes."""

    MATCHED = "matched"
    DIVERGED = "diverged"
    DIVERGED_UNKNOWN = "diverged_unknown"


class CounterfactualPrefixError(ValueError):
    """The historical stream — and therefore every prefix of it — is invalid."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CounterfactualResult:
    """Honest report of one counterfactual re-drive.

    ``redriven_status`` is ``None`` exactly when the outcome is
    ``DIVERGED_UNKNOWN``: an aborted re-drive has no honest settled state
    to report. ``first_divergence_sequence`` is set exactly when the
    outcome is ``DIVERGED``.
    """

    run_id: RunId
    outcome: ReplayOutcome
    prefix_length: int
    historical_length: int
    redriven_length: int
    historical_status: RunStatus
    redriven_status: RunStatus | None
    first_divergence_sequence: int | None
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReplayOutcome):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "outcome must be a ReplayOutcome"
            raise TypeError(msg)
        if min(self.prefix_length, self.historical_length, self.redriven_length) < 1:
            msg_2 = "stream lengths must be positive"
            raise ValueError(msg_2)
        if self.prefix_length > self.historical_length:
            msg_3 = "prefix_length cannot exceed historical_length"
            raise ValueError(msg_3)
        if self.outcome is ReplayOutcome.DIVERGED_UNKNOWN and self.redriven_status is not None:
            msg_4 = "an unknown outcome has no honest redriven status"
            raise ValueError(msg_4)
        if self.outcome is ReplayOutcome.DIVERGED and self.first_divergence_sequence is None:
            msg_5 = "a diverged outcome must name its first divergence sequence"
            raise ValueError(msg_5)
        if (
            self.outcome is not ReplayOutcome.DIVERGED
            and self.first_divergence_sequence is not None
        ):
            msg_6 = "only a diverged outcome may name a first divergence sequence"
            raise ValueError(msg_6)
        if not isinstance(self.detail, str) or not self.detail.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_7 = "detail must be a non-empty honest explanation"
            raise ValueError(msg_7)


@dataclass(frozen=True, slots=True)
class ScriptedTurns:
    """The recorded per-turn decisions of a historical run, in stream order."""

    proposals: tuple[ActionProposal, ...]
    tool_results: tuple[ToolResult, ...]
    tool_metadata: tuple[ToolMetadata, ...]
    verifications: tuple[VerificationResult, ...]


def extract_scripted_turns(events: tuple[Event, ...]) -> ScriptedTurns:
    """Reconstruct scripted-adapter inputs from a recorded stream.

    The re-driven runtime replays exactly what the historical run
    proposed, observed, and verified, so any suffix difference is
    attributable to the knobs under study — never to invented model or
    tool behavior.
    """
    proposals = tuple(event.proposal for event in events if isinstance(event, ActionProposed))
    results: list[ToolResult] = []
    for event in events:
        if isinstance(event, ToolSucceeded):
            results.append(ToolResult(ok=True, observation=event.observation))
        elif isinstance(event, ToolFailed):
            results.append(
                ToolResult(
                    ok=False,
                    observation=event.error_message,
                    error_code=event.error_code,
                    failure_class=event.failure_class,
                )
            )
    metadata: dict[str, ToolMetadata] = {}
    for event in events:
        if isinstance(event, ActionAuthorized):
            metadata.setdefault(event.tool_metadata.name, event.tool_metadata)
    verifications = tuple(
        VerificationResult(passed=True, summary=event.summary)
        if isinstance(event, VerificationPassed)
        else VerificationResult(passed=False, summary=event.summary, score=event.score)
        for event in events
        if isinstance(event, (VerificationPassed, VerificationFailed))
    )
    return ScriptedTurns(
        proposals=proposals,
        tool_results=tuple(results),
        tool_metadata=tuple(metadata.values()),
        verifications=verifications,
    )


def _canonical_events(
    events: tuple[Event, ...],
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Behavior-only comparison projection.

    Volatile envelope fields (ids, timestamps, sequence numbers, causal
    links) and evidence-only shadow decisions are excluded: they are not
    run behavior, so they can never manufacture a divergence.
    """
    canonical: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for event in events:
        if isinstance(event, ShadowDecisionRecorded):
            continue
        body = tuple(
            sorted(
                (field.name, repr(getattr(event, field.name)))
                for field in fields(event)
                if field.name not in _VOLATILE_FIELDS
            )
        )
        canonical.append((type(event).__name__, body))
    return tuple(canonical)


def counterfactual_redrive(  # noqa: PLR0912 - one validation/drive/compare flow
    *,
    runtime: Runtime,
    run_id: RunId,
    historical: tuple[Event, ...],
    prefix_length: int,
) -> CounterfactualResult:
    """Re-drive ``run_id`` from a prefix of its historical stream.

    ``runtime`` must be bound to a fresh store (no events for ``run_id``);
    the prefix is seeded into that store and the runtime is resumed until
    the run settles (terminal or waiting for approval). The re-driven
    suffix is then compared against the historical suffix under the
    behavior-only canonical projection.
    """
    if not historical:
        msg = "historical stream is empty"
        raise CounterfactualPrefixError(msg)
    if not 1 <= prefix_length <= len(historical):
        msg_2 = "prefix_length must be within 1..len(historical)"
        raise ValueError(msg_2)
    try:
        historical_status = replay(run_id, historical).status
    except (ValueError, InvalidTransitionError) as exc:
        msg_3 = f"historical stream fails domain validation: {exc}"
        raise CounterfactualPrefixError(msg_3) from exc
    store = runtime.store
    if store.current_version(run_id) != 0:
        msg_4 = "counterfactual redrive requires a fresh store for the run"
        raise ValueError(msg_4)
    prefix = historical[:prefix_length]
    for event in prefix:
        store.append(event, expected_version=event.sequence - 1)
    # A prefix of an already-validated stream always replays cleanly.
    prefix_status = replay(run_id, prefix).status
    settled: RunStatus | None = prefix_status
    aborted: str | None = None
    if not (prefix_status.is_terminal or prefix_status is RunStatus.WAITING_FOR_APPROVAL):
        try:
            settled = runtime.resume(run_id).status
        except UnsafeResumeStateError as exc:
            aborted = f"prefix cannot resume safely without re-executing side effects: {exc}"
        except Exception as exc:  # analysis tooling: any redrive failure is an honest UNKNOWN
            aborted = f"redrive aborted with {type(exc).__name__}: {exc}"
    redriven_events = store.events_for(run_id)
    if aborted is not None:
        return CounterfactualResult(
            run_id=run_id,
            outcome=ReplayOutcome.DIVERGED_UNKNOWN,
            prefix_length=prefix_length,
            historical_length=len(historical),
            redriven_length=len(redriven_events),
            historical_status=historical_status,
            redriven_status=None,
            first_divergence_sequence=None,
            detail=aborted,
        )
    assert settled is not None  # resume() always returns the settled state
    historical_suffix = _canonical_events(historical[prefix_length:])
    redriven_suffix = _canonical_events(redriven_events[prefix_length:])
    shared = min(len(historical_suffix), len(redriven_suffix))
    divergence = next(
        (index for index in range(shared) if historical_suffix[index] != redriven_suffix[index]),
        None,
    )
    if divergence is None and len(historical_suffix) != len(redriven_suffix):
        divergence = shared
    if divergence is None:
        return CounterfactualResult(
            run_id=run_id,
            outcome=ReplayOutcome.MATCHED,
            prefix_length=prefix_length,
            historical_length=len(historical),
            redriven_length=len(redriven_events),
            historical_status=historical_status,
            redriven_status=settled,
            first_divergence_sequence=None,
            detail="redriven suffix is canonically identical to the historical suffix",
        )
    sequence = prefix_length + divergence + 1
    if divergence < len(historical_suffix) and divergence < len(redriven_suffix):
        historical_event = historical_suffix[divergence]
        redriven_event = redriven_suffix[divergence]
        detail = (
            f"suffixes diverge at sequence {sequence}: "
            f"historical {historical_event[0]} vs redriven {redriven_event[0]}"
        )
        if historical_event[0] == redriven_event[0]:
            differing = next(
                (
                    name
                    for (name, value), (_, other) in zip(
                        historical_event[1], redriven_event[1], strict=True
                    )
                    if value != other
                ),
                None,
            )
            if differing is not None:
                detail = f"{detail} (field {differing!r} differs)"
    else:
        detail = (
            f"suffixes diverge at sequence {sequence}: historical suffix has "
            f"{len(historical_suffix)} events, redriven suffix has {len(redriven_suffix)}"
        )
    return CounterfactualResult(
        run_id=run_id,
        outcome=ReplayOutcome.DIVERGED,
        prefix_length=prefix_length,
        historical_length=len(historical),
        redriven_length=len(redriven_events),
        historical_status=historical_status,
        redriven_status=settled,
        first_divergence_sequence=sequence,
        detail=detail,
    )
