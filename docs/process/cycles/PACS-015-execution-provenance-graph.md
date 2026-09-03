# PACS-015: Execution provenance graph and trajectory debugger

Status: CLOSED (2026-09-03)

## Objective

Give the operator a replayable answer to "why did the system take this
action?" for any step of any run: an execution provenance DAG derived from
the authoritative event stream, an explain surface over it, durable run
lineage for follow-up sessions, and a force-release escape hatch for zombie
runs — exposed over REST and in the operator console.

## Operator decisions (2026-09-02)

1. Event catalog grows 24→25 (signed off): `ModelTurnRecorded` records
   per-turn model identity as evidence-only durable events.
2. Force-release is IN SCOPE for this cycle (zombie repository claims need
   an operator escape hatch).
3. A dedicated PG test database is IN SCOPE (the PACS-014b finding: the
   integration suite must never again be able to wipe live sessions);
   exploration-vs-no-progress tuning is DEFERRED.
4. Provenance is a pure on-demand projection — no persisted graph, no
   cache, no divergence risk (ADR-0011).
5. `RunStarted.parent_run_id` is an optional payload field (signed off);
   SCHEMA_VERSION stays 1 and pre-PACS-015 streams replay byte-identical.
6. `DomainEvent.caused_by` stays dormant — this cycle proves how far
   honest derivation goes without recorded causal IDs.

## Milestones

- **M1** `f2d6bf3` — PG integration suite isolated behind a dedicated
  `loopforge_test` database: `fresh_schema` fails closed unless the dbname
  ends with `_test`; pointing the suite at the production DSN now errors
  loudly on all 17 tests. Production DB (103 events) verified untouched.
- **M2** `f481785` — `ModelTurnRecorded(provider, model, action_id)`
  through all five touchpoints (domain catalog, reducer evidence-only arm,
  codec, telemetry log arm, runtime append after every successful model
  turn) plus the UI wire mirror. Mid-run model swaps are attributed; a
  failed turn records nothing; pre-015 streams replay byte-identical.
- **M3** `be70dd6` — provenance DAG + explain: closed node/edge vocabulary
  (no thought/CoT kinds, pinned), single-pass deterministic builder
  (double-build pinned), explain's causal spine restricted to forward edge
  kinds (associative edges never hijack the spine), cycle-safe walks,
  patch-traceability acceptance pin on a 23-event repair stream.
- **M4** `efdafa3` — durable run lineage: `RunStarted.parent_run_id`
  payload, reducer projection, `follow_up` stamps the successor,
  `SessionManager.lineage` resolves ancestors (bounded, forged-cycle-safe)
  and descendants from wiring + replay. Legacy registry wirings decode
  with `None`.
- **M5** `cbed6b9` — force-release: `application/maintenance.py::
  force_stop_run` appends `RunStopped(CANCELLED)` compare-and-append at
  the current stream version WITHOUT replaying (works on corrupted
  streams; a racing writer fails it closed). `SessionManager.
  force_release` denies driving/terminal/unknown; REST `POST
  .../force-release` requires a literal JSON `confirm: true` (StrictBool —
  `1`, `"true"`, and omission are all 422).
- **M6** `113c282` — REST projections: `GET .../provenance`,
  `GET .../explain?node=`, `GET .../lineage`; unknown node → 404 via the
  existing `UnknownProvenanceNodeError` surface, missing param → 422,
  unknown run → 404; double-GET determinism pinned.
- **M7** `e0b19b4` — console: typed api.ts clients, collapsible
  provenance panel (derived on demand, refreshed with the stream only
  while open) with inline explain chain (spine → focus → outcomes,
  clickable rows walk the DAG), lineage header links via
  `navigateToSession`, and a force-release control whose confirm button
  is gated on an explicit "bypass replay checks" checkbox.
- **M8** (this record) — live validation, below.
- **M9** (this record) — ADR-0011, cycle record, HANDOFF checkpoint,
  ui/README note.

## Adversarial review (dedicated pass after M7; all findings fixed and pinned in `d945bdd`)

1. **HIGH — stale trigger attribution.** Verification/reflection trigger
   pointers were never consumed, so a spent signal was re-attributed as
   the TRIGGERED cause of every later turn (approval rejection, operator
   instruction, or mid-loop tool result in between). Fixed with consumable
   attribution (`pending_trigger_id`); spent turns fall back to the
   requirement. Confirmed live: the M8 run's pre-fix graph showed the
   fabricated shape; post-fix the same durable stream does not.
2. **HIGH — unconditional stop attribution.** `RunStopped` gained a
   RESULTED_IN edge from the last verification regardless of reason, so
   cancelled/budget stops were "explained" as caused by a verification.
   Now gated on `SUCCESS_VERIFIED`; other reasons record honest absence.
   Live before/after on run `run_c738d7ad3645`: pre-fix
   `verification:47 → stop:52`; post-fix no such edge.
3. **MEDIUM — decode failures misreported on read routes.** Codec
   `ValueError` subclasses mapped to 422 (blaming a parameterless GET) and
   the `TypeError` family escaped as bare plain-text 500s. Both families
   now land in the consistent `{"detail": ...}` 500 mapping, registered
   before the generic `ValueError` handler.
4. **MEDIUM — `force_release` swallowed transient store errors.** The
   broad `except Exception` treated I/O/locking failures as "corrupted
   stream", skipping the deny-checks and appending anyway. Narrowed to
   replay/decode failures (`InvalidTransitionError`, `TypeError`,
   `ValueError`); transient errors propagate with nothing appended.
5. Checked and dismissed (clean or already pinned): determinism, spine
   cycle-safety, SEQUENCE backbone off-by-one, action_id reuse across
   bundle rebuilds, summary truncation collisions, StrictBool layering,
   driving/terminal/unknown denials, the CAS race, registry corruption
   handling, and unknown-event-type tolerance in the UI (`describeEvent`
   default branch).

## Live validation (2026-09-03; real PG + Ollama `devstral-small-2:latest` + container sandbox)

- Scratch target: blackjack-demo cloned to `/tmp/lf-m8-blackjack` at the
  seeded-defect commit `3be911f` (3 failing tests, `blackjack-checks:local`
  image). Server: Postgres DSN, port 8377, `/tmp/lf-m8-server` data dir.
- **Run `run_c738d7ad3645`** drove 5 iterations (52 events) to
  `budget_exhausted`: 6 `ModelTurnRecorded` events with real model
  identity, 52-node/101-edge provenance graph, explain chains rooted at
  the requirement. Console evidence: `output/playwright/
  m8-provenance-panel.png`, `m8-explain-chain.png` (lineage "child →"
  link visible in the detail grid).
- **Lineage both directions:** follow-up `run_3f3d4990862f` carries
  `parent_run_id` in its durable `RunStarted`; `/lineage` on child and
  root agree (`ancestors`/`children`).
- **Zombie drill:** started driving the follow-up, killed the server
  mid-drive, restarted: run non-terminal, driver gone, new session on the
  same repo → 409. `force-release` without `confirm` → 422; with
  `confirm: true` → 200, run `cancelled`, new session → 201.
- **M1 regression:** the PG integration suite (17 tests) ran against
  `loopforge_test` while production held the drill data; production event
  count before/after the suite identical (189 = 103 pre-cycle + 86 drill
  events).

## Acceptance gates

- Rebuildable graph from the event log ✓ (M3 determinism pin; pure
  projection, ADR-0011)
- Patch traceable to model output, tool result, verifier outcome ✓ (M3
  patch-traceability pin on the 23-event repair stream; live graph in M8)
- No hidden chain-of-thought ✓ (closed vocabulary pinned; no thought kinds)
- Deterministic reconstruction ✓ (M3 double-build pin; M6 double-GET pin)
- Console exposes provenance/explain ✓ (M7 panel + M8 screenshots)
- Test-count progression: 1634 → 1709 passed / 18 skipped (platform-gated),
  branch coverage ≥ 95% ≥ 90 floor; ruff format+check, pyright strict,
  lint-imports, UI tsc+vite all green at every milestone.

## Deferred

- Exploration-vs-no-progress tuning (operator decision 3).
- `caused_by` recorded causal IDs (schema-v2 candidate; ADR-0011 §5).
- Provenance caching/materialization (rejected for now — divergence risk,
  ADR-0011 consequences).
