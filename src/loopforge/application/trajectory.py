"""Trajectory-quality metrics as pure projections of the event stream (PACS-016, M4).

``compute_trajectory_metrics`` fills every field of the domain
``TrajectoryMetrics`` from one trial's authoritative durable event stream plus
caller-supplied wiring-time data. It is a pure, deterministic projection: the
same stream and the same caller data always yield the same metrics, and no
metric is ever inferred from model self-report — only from durable events and
operator-owned configuration.

Provenance reuse, considered and rejected (auditable decision): the PACS-015
provenance graph (``loopforge.application.provenance``) was evaluated as a
source for these counts. Its value is CAUSAL STRUCTURE — typed edges answering
"why did this happen" — not counting. The metrics here are plain tallies and
ratios over event payloads; routing them through a derived graph would add an
indirection that can only lose information (the graph deliberately drops
payload detail into summaries) without adding evidence. Direct stream scans
are therefore the honest derivation, consistent with the M3 graders.

Honesty notes per derivation (what is exact, what is an estimate):

- ``model_turns`` / ``permission_requests`` / ``recovery_events``: exact
  counts of durable event types — no estimation.
- ``repetition_ratio``: EXACT duplicates only (canonical tool+arguments
  signature). Near-misses and paraphrased retries are NOT counted; this is a
  deliberate honesty-over-cleverness choice, documented in the function.
- ``expensive_model_turns``: exact count, but the expensive SET is
  wiring-time caller data. This module stays provider-agnostic: tier
  classification (``ModelTier``/cost rates) belongs to the M5 runner that
  knows the configured registry, not to this projection.
- ``scope_violations``: exact count of well-formed out-of-scope proposals,
  judged with the code-owned ``PatchConstraints`` path semantics. Proposals
  whose path argument is missing or not a valid relative path are NOT
  counted: those are rejected at the schema/tool boundary, and this metric
  measures discipline (in-scope vs out-of-scope intent), not malformed
  output.
- ``context_tokens_used``: two-source precedence — caller-supplied
  ``ContextAccounting`` ledgers (the runtime's own budgeting counter, an
  ESTIMATE owned by the runtime, not a provider tokenizer) win when present;
  otherwise the peak durable ``BudgetDebited`` ``input_tokens`` (the real
  billed prompt size). See the function docstring for the full precedence
  contract.
- ``context_items_dropped``: only knowable from caller-supplied accountings;
  the durable event stream does NOT carry compaction/drop detail, so the
  metric is honestly 0 when no accountings are supplied.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from loopforge.domain.benchmarks import BenchmarkTaskSpec, TrajectoryMetrics
from loopforge.domain.context_lifecycle import ContextAccounting
from loopforge.domain.events import (
    ActionProposed,
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    BudgetDebited,
    CircuitOpened,
    Event,
    ModelTurnRecorded,
    OperatorInstruction,
    ReflectionRecorded,
    RetryScheduled,
    ToolFailed,
)
from loopforge.domain.workspace import PatchConstraints

FILE_MUTATING_PATH_ARGUMENTS: Mapping[str, str] = {
    "write_file": "path",
    "edit_file": "path",
    "revert_file": "path",
}
"""Default tool-name → path-argument mapping for scope-violation detection.

Pinned against the repair workload's code-owned tool specs
(``loopforge.workloads.repair``): the file-mutating tools ``write_file``
(path, content), ``edit_file`` (path, old, new), and ``revert_file`` (path)
all carry their target in the ``"path"`` argument. Read-only tools
(``read_file``, ``search_files``, ``workspace_*``) and sandbox commands are
deliberately absent: reading out-of-scope content or running a code-owned
allowlisted command is not a patch-scope violation. Other workloads override
this via the ``path_arguments`` keyword.
"""


def _proposal_signature(event: ActionProposed) -> tuple[str, str]:
    """Canonical identity of one proposal: tool name + sorted-key JSON arguments.

    ``json.dumps(..., sort_keys=True)`` makes the signature insensitive to
    mapping insertion order, so two proposals that differ only in argument
    ordering are exact duplicates — while any real argument difference
    (including same tool, different content) is not.
    """
    proposal = event.proposal
    canonical = json.dumps(dict(proposal.arguments), sort_keys=True, separators=(",", ":"))
    return (proposal.tool_name, canonical)


def _repetition_ratio(events: tuple[Event, ...]) -> float:
    """Fraction of proposals duplicating an earlier proposal in the same run.

    A proposal counts once per repeated occurrence: three identical proposals
    yield 2 duplicates out of 3 proposals (ratio 2/3). Exact duplicates only —
    no fuzzy matching — so the ratio never claims to detect "the model tried
    roughly the same thing". 0.0 when the stream carries no proposals.
    """
    seen: set[tuple[str, str]] = set()
    proposals = 0
    duplicates = 0
    for event in events:
        if not isinstance(event, ActionProposed):
            continue
        proposals += 1
        signature = _proposal_signature(event)
        if signature in seen:
            duplicates += 1
        else:
            seen.add(signature)
    if proposals == 0:
        return 0.0
    return duplicates / proposals


def _is_valid_relative_path(path: str) -> bool:
    """Whether ``path`` passes the code-owned relative-path contract.

    Reuses ``PatchConstraints`` validation (``_validate_relative_path``
    semantics: relative, no backslashes/control characters, no empty/``.``/
    ``..`` segments, never the Git metadata directory) as the single source
    of path truth, so this metric's notion of "well-formed path" cannot drift
    from the workspace contract.
    """
    try:
        PatchConstraints(allowed_prefixes=(path,))
    except ValueError:
        return False
    return True


def _path_allowed(path: str, constraints: PatchConstraints) -> bool:
    """Exact ``PatchConstraints`` scope semantics: empty prefixes allow all.

    Mirrors the verifier's and graders' code-owned rule (a path is allowed
    when it equals a prefix or lives under it), so proposal-scope checks
    share precisely the patch-scope contract.
    """
    if not constraints.allowed_prefixes:
        return True
    return any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/")
        for prefix in constraints.allowed_prefixes
    )


def _scope_violations(
    spec: BenchmarkTaskSpec,
    events: tuple[Event, ...],
    path_arguments: Mapping[str, str],
) -> int:
    """Count file-mutating proposals whose well-formed path escapes the scope.

    A proposal is a violation iff its tool is in ``path_arguments``, the
    mapped argument exists, is a valid relative path, and is outside
    ``spec.allowed_prefixes``. Empty prefixes allow all (0 violations).
    Missing arguments and invalid path values (absolute paths, ``..``
    traversal, backslashes) are not counted: those fail at the schema/tool
    boundary and this metric measures scope discipline, not malformed output.
    """
    constraints = PatchConstraints(allowed_prefixes=tuple(spec.allowed_prefixes))
    if not constraints.allowed_prefixes:
        return 0
    violations = 0
    for event in events:
        if not isinstance(event, ActionProposed):
            continue
        argument = path_arguments.get(event.proposal.tool_name)
        if argument is None:
            continue
        path = event.proposal.arguments.get(argument)
        if path is None or not _is_valid_relative_path(path):
            continue
        if not _path_allowed(path, constraints):
            violations += 1
    return violations


def _context_tokens_used(
    events: tuple[Event, ...], context_accountings: tuple[ContextAccounting, ...]
) -> int:
    """Peak context size, preferring accounting ledgers over billed usage.

    Precedence: when the caller supplies per-assembly ``ContextAccounting``
    ledgers (collected via the ``ContextAccountingSource`` seam), the peak
    ``used_tokens`` across them wins — it is the runtime's own budgeting
    ledger, an ESTIMATE from the runtime-owned token counter (not a provider
    tokenizer) but the only source that reflects selection/compaction.
    Otherwise the peak ``BudgetDebited`` ``usage.input_tokens`` is the
    fallback: the real billed prompt size, exact but blind to dropped items.
    0 when neither source carries data.
    """
    if context_accountings:
        return max(accounting.used_tokens for accounting in context_accountings)
    peaks = [event.usage.input_tokens for event in events if isinstance(event, BudgetDebited)]
    return max(peaks) if peaks else 0


def _context_items_dropped(context_accountings: tuple[ContextAccounting, ...]) -> int:
    """Items excluded from the final context, summed over supplied accountings.

    Counts ``dropped_entries`` (excluded by role, expiry, supersession, or
    budget) plus kept-but-compacted entries — a distinct population in
    ``ContextAccounting`` (compaction truncates content without dropping the
    item), included here because truncated content is partially lost context.
    Honestly 0 when no accountings are supplied: the durable event stream
    does not carry compaction/drop detail, so this module never fabricates it.
    """
    dropped = 0
    for accounting in context_accountings:
        dropped += len(accounting.dropped_entries)
        dropped += sum(1 for entry in accounting.kept_entries if entry.compacted)
    return dropped


def compute_trajectory_metrics(
    spec: BenchmarkTaskSpec,
    events: tuple[Event, ...],
    *,
    expensive_models: frozenset[tuple[str, str]] = frozenset(),
    context_accountings: tuple[ContextAccounting, ...] = (),
    path_arguments: Mapping[str, str] = FILE_MUTATING_PATH_ARGUMENTS,
) -> TrajectoryMetrics:
    """Project one trial's durable stream into its ``TrajectoryMetrics``.

    Pure and deterministic. ``spec`` supplies only the operator-owned scope
    contract (``allowed_prefixes``); every other derivation reads the durable
    stream or explicit caller data:

    - ``model_turns``: count of ``ModelTurnRecorded`` (successful turns only —
      transient model retries are never durable).
    - ``repetition_ratio``: exact-duplicate proposal fraction (see
      ``_repetition_ratio``); 0.0 with no proposals.
    - ``expensive_model_turns``: count of ``ModelTurnRecorded`` whose
      ``(provider, model)`` is in ``expensive_models``. The set is wiring-time
      data — the M5 runner derives it from the configured ``ModelTier``/cost
      rates — so this projection stays provider-agnostic.
    - ``scope_violations``: out-of-scope file-mutating proposals under
      ``PatchConstraints`` semantics (see ``_scope_violations``);
      ``path_arguments`` overrides the tool→path-argument mapping for other
      workloads.
    - ``permission_requests``: count of ``ApprovalRequested`` (grants and
      rejections are human interventions, not requests).
    - ``context_tokens_used`` / ``context_items_dropped``: see their helpers
      for the two-source precedence and the honest-0 fallback.
    - ``recovery_events``: count of ``RetryScheduled`` + ``CircuitOpened`` +
      ``ToolFailed`` (every failure class) + ``ReflectionRecorded``.
    """
    model_turns = 0
    expensive_turns = 0
    for event in events:
        if not isinstance(event, ModelTurnRecorded):
            continue
        model_turns += 1
        if (event.provider, event.model) in expensive_models:
            expensive_turns += 1
    recovery = sum(
        1
        for event in events
        if isinstance(event, RetryScheduled | CircuitOpened | ToolFailed | ReflectionRecorded)
    )
    return TrajectoryMetrics(
        model_turns=model_turns,
        repetition_ratio=_repetition_ratio(events),
        expensive_model_turns=expensive_turns,
        scope_violations=_scope_violations(spec, events, path_arguments),
        permission_requests=sum(1 for event in events if isinstance(event, ApprovalRequested)),
        context_tokens_used=_context_tokens_used(events, context_accountings),
        context_items_dropped=_context_items_dropped(context_accountings),
        recovery_events=recovery,
    )


def count_human_interventions(events: tuple[Event, ...]) -> int:
    """Count durable human-in-the-loop interventions in one run's stream.

    ``ApprovalGranted`` + ``ApprovalRejected`` + ``OperatorInstruction``: every
    recorded moment a human exercised authority over the run. Approval
    REQUESTS are not interventions (they are the harness asking); the M5
    runner uses this for ``TrialRecord.human_interventions``.
    """
    return sum(
        1
        for event in events
        if isinstance(event, ApprovalGranted | ApprovalRejected | OperatorInstruction)
    )
