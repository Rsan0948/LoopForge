# ADR-0013 — Verification cadence skips successful read-only turns

Status: Accepted

## Context

PACS-014b documented a live defect (finding 2): the runtime verified after
*every* turn, including read-only exploration. Because the reducer's
progress semantics count only verification outcomes, an unchanged workspace
produced an identical verification score, so `consecutive_no_progress`
accrued on every exploratory turn and `STOP_STALLED_NO_PROGRESS` killed
legitimate exploration at the default limit of 3 — exploration was
indistinguishable from flailing. PACS-014b made the limit a knob (symptom
relief); PACS-016 M8 (operator-approved fold-in of the PACS-015 deferred
item) fixes the cause.

Two constraints shaped the fix:

1. **Replay compatibility.** The reducer's `_verification_progress`
   semantics are pinned by property tests and replay byte-identity
   guarantees for pre-existing streams. Changing what the reducer counts as
   progress would alter the meaning of every historical stream.
2. **Authority purity.** "Read-only" must be a code-owned fact. If the
   model could influence whether its turn verifies, the skip would be a
   verification-evasion channel (AGENTS.md rules 4, 12).

## Decision

1. **Verification is a runtime cadence concern, not a reducer concern.**
   `Runtime.verify_read_only_turns` (code-owned, default `False`) skips
   verification after a turn whose completed action is READ-only per the
   action's authorized `ToolMetadata` permission class — metadata the model
   can never define or downgrade (rule 4). The reducer's progress semantics
   are untouched; the only reducer change is additive (`PlanCreated`
   admitted from `VERIFYING`, a position only the skip produces), so
   pre-PACS-016 streams replay byte-identical and the schema-v1 catalog
   stays at 25 events.
2. **Absence of a verification event is honest evidence.** A skipped turn
   durably records no verification — the stream's own `ActionAuthorized`
   metadata explains why — and the runtime re-plans directly out of
   VERIFYING with a code-owned skip plan. Provenance, graders, and the
   trajectory projection already tolerate (and pin) honest absence.
3. **Only successful read-only turns skip.** Failed read-only actions
   verify exactly as before, so retry/circuit bookkeeping and failure
   evidence are unaffected; every non-READ action verifies identically in
   both modes; flailing with repeated writes still stalls in both modes.
4. **The legacy cadence remains selectable** (`verify_read_only_turns=True`)
   so the evaluation laboratory can A/B the regimes — the tuning landed
   with before/after evidence (legacy stalls every exploring trial; tuned
   solves them), not with intuition.
5. **Verifier contract recorded honestly.** The skip assumes verdicts are
   invariant to successful read-only turns — true for workspace-truth
   verifiers (`RepairVerifier`), false for observation-based verifiers
   (e.g. `ObservationContainsVerifier`), which must wire
   `verify_read_only_turns=True`. This constraint is documented on the
   knob; existing observation-verifier tests pin the legacy mode.

## Consequences

Positive:

- exploration no longer burns full verification runs nor accrues stall
  strikes — the exact PACS-014b live failure (six read-only turns before a
  real success) now succeeds by default;
- streams get smaller and cheaper for read-heavy runs, with no loss of
  information (nothing changed to verify);
- the change is fully reversible per-runtime and A/B-measurable through
  the evaluation laboratory.

Negative:

- pure-read flailing no longer accrues no-progress strikes; it is bounded
  by the iteration budget and stops `MAX_ITERATIONS` instead of `STALLED`
  (still bounded, still honest — but the stall metric no longer counts it);
- routing stall escalation (`consecutive_no_progress` threshold) triggers
  later on exploration-heavy runs;
- observation-based verifiers silently regress under the tuned default if
  wired without the legacy flag — mitigated by documentation and the
  existing legacy-mode pins, but the port contract constraint is
  convention, not a type-level guarantee.
