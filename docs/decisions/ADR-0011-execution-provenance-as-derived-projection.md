# ADR-0011 — Execution provenance as a pure derived projection (no persisted graph, no causal IDs)

Status: Accepted

## Context

PACS-015 (execution provenance graph and trajectory debugger) needs to answer
"why did the system take this action?" for any step of any run — including
runs that ended badly. Two design pressures shaped the decision:

1. **Authority purity.** The event stream is the only authoritative state. A
   second, separately-persisted provenance store would either duplicate
   authority (two things to keep consistent, two things that can diverge) or
   admit that the stream is not sufficient — in which case the stream should
   be fixed instead.
2. **Honesty about causality.** The runtime does not currently record
   explicit causal links between events (`DomainEvent.caused_by` exists but
   is dormant — never set by production code). Any provenance surface built
   today must derive relationships from what the stream durably contains:
   sequence order and code-owned correlation identifiers (action lifecycles
   by `action_id`, turn structure, verification/evidence adjacency). It must
   never present inference as fact, and it must never record model
   "reasoning" — the node vocabulary deliberately has no thought/CoT kinds.

A related operational need: servers that die mid-drive leave non-terminal
"zombie" runs holding their repository claim, and `Runtime.cancel` cannot
help when the stream no longer replays — it replays first.

## Decision

1. **Provenance is a pure on-demand projection.** `build_provenance_graph`
   derives an immutable DAG (`domain/provenance.py` closed vocabulary,
   `application/provenance.py` single-pass builder) from one run's event
   tuple, every time, with nothing persisted. The same stream always yields
   the identical graph (pinned by double-build determinism). `explain`
   answers "why did this node happen?" from the derived graph alone: a
   causal spine over forward edge kinds plus typed supporting evidence.
2. **Edges are derived, never inferred-and-asserted.** Every edge kind maps
   to a durable, code-owned correlation rule. Signals used for attribution
   are *consumable* (a verification/reflection triggers exactly the next
   model turn) and reason-gated (only `SUCCESS_VERIFIED` stops claim a
   verification cause) — re-attributing a spent signal fabricates causality
   the stream contradicts (found in adversarial review; fixed and pinned).
   Where no durable correlation exists, the graph records an honest absence.
3. **Run lineage is a payload field, not a new event.** `RunStarted` gained
   an optional `parent_run_id` (schema stays v1; absent decodes to `None`,
   so pre-PACS-015 streams replay byte-identical). `follow_up` stamps it;
   ancestry is resolved on demand from wiring plus replay, with bounded,
   cycle-safe walks.
4. **Force-release is a replay-free compare-and-append, with the operator
   as the liveness check.** `force_stop_run` appends
   `RunStopped(CANCELLED)` at `expected_version=current_version` without
   replaying, so it works on streams the reducer can no longer validate.
   There is deliberately NO cross-process liveness marker: nothing in the
   system can tell a wedged driver from a slow one, so the command denies
   the mechanically checkable cases (live driver, terminal run, unknown
   run, missing literal `confirm: true`) and lets the compare-and-append
   fail closed against a racing writer. A corrupted stream stays broken
   afterwards — only the claim is released; history is never rewritten.
5. **`caused_by` stays dormant.** Explicit causal IDs remain a possible
   future schema-v2 decision; this cycle proves how far honest derivation
   goes without them.

## Consequences

Positive:

- the provenance surface can never diverge from the authoritative stream —
  there is nothing to sync, migrate, or rebuild;
- graph improvements (like the adversarial-review fixes) apply retroactively
  to every historical run, because the graph is recomputed on every read;
- the vocabulary constraint is enforceable: node/edge kinds are closed
  enums, pinned against thought/recording kinds, so the surface cannot
  drift into recording model chain-of-thought;
- force-release works exactly where replay-based commands fail, and its
  guarantees are mechanical (CAS) rather than aspirational.

Negative:

- derivation is O(stream length) on every read; very long runs pay it per
  request (acceptable for an operator console; a cache would reintroduce
  the divergence risk this decision rejects);
- derived attribution is weaker than recorded causality: mid-loop turns
  fall back to `requirement`-triggered edges once a signal is spent. This
  is the honest floor, not a bug — but a future schema-v2 `caused_by` could
  sharpen it;
- force-release trusts the operator's liveness judgment: a misdiagnosed
  "zombie" that is actually a slow live driver in another process is only
  protected by the CAS at append time, not prevented.
