# PACS-006 — Context authority model

Status: SUCCESS

## Objective

Make model context a first-class, typed, provenance-aware runtime artifact rather than an ad hoc
collection of strings (previously: the raw `RunState` projection passed straight to the model).

## Why now

PACS-001–005 established the invariant-bearing kernel, durable event store, reliability control
plane, fault-injection laboratory, and sandbox/security contract. Every later cycle that touches
models (PACS-007 context lifecycle, PACS-008 observability, PACS-011 live adapter) needs context
to be a governed, typed boundary artifact first. The `TrustClass` vocabulary landed dormant in
PACS-005 precisely to enable this cycle.

## Dependencies

- PACS-001–005 (all SUCCESS).

## In scope

- `ContextItem` / `ModelContext` domain vocabulary (`domain/context.py`);
- explicit source/provenance metadata (`ContextSource`);
- trust/authority classes separating runtime policy, authorized human requirements, deterministic
  observations, retrieved material, model inference, and untrusted content (reusing the PACS-005
  `TrustClass` vocabulary);
- supersession and freshness semantics (`supersedes`, `expires_at`, `active_items`);
- sensitivity metadata reusing `DataSensitivity`, compatible with later telemetry redaction;
- `ContextBuilderPort` boundary (`ports/context.py`);
- rules preventing lower-trust content from silently becoming higher-authority policy
  (`TRUST_AUTHORITY` ordering + guarded `promote`);
- runtime seam: the model boundary now takes `ModelContext`, and every model turn is preceded by a
  durable `ContextAssembled` event.

## Out of scope

- compaction optimization, prompt caching, live models (per cycle spec);
- role-specific assembly, token budgeting, context selection (PACS-007);
- telemetry redaction pipeline (PACS-008) — see SECRET persistence guard below.

## Plan

1. Domain vocabulary + authority/elevation rules in `domain/context.py`.
2. `ContextBuilderPort` protocol in `ports/context.py`.
3. `ContextAssembled` durable event (codec-registered, strict decode) so provenance/trust survive
   serialization and replay.
4. Runtime wiring: `Runtime.context` builder, boundary validation, `ContextAssembled` persistence,
   `ModelPort.propose_action(context: ModelContext)`.
5. `BasicContextBuilder` deterministic adapter; scripted adapters + CLI updated.
6. Deterministic tests: authority ordering, supersession/freshness, invalid elevation, boundary
   contract, codec round-trip, runtime integration.
7. Reconcile roadmap/build-status/handoff/AGENTS documentation.

## Act

Implemented as planned. Notable decisions and discoveries:

- `TrustClass` was reused in place from `domain/security.py` (vocabulary pinned by existing
  stability test) rather than moved; `domain/context.py` imports it.
- `ContextItem.trust` must equal `ContextSource.origin` at construction: trust cannot disagree
  with recorded provenance.
- `promote(item, to=..., basis=...)` is the only elevation path. It returns a new item with new
  provenance; demotion always succeeds; `UNTRUSTED_CONTENT` and `MODEL_INFERENCE` can never be
  promoted to `RUNTIME_POLICY` or `AUTHORIZED_HUMAN`; every promotion requires a non-empty basis
  reference. Nothing in the runtime calls `promote` yet — it exists so later cycles cannot elevate
  silently.
- `ContextItemSnapshot` (the event payload) rejects `DataSensitivity.SECRET` at construction:
  secret-sensitivity content must never enter the durable event store. Redaction of lesser
  sensitivities before telemetry export remains PACS-008 scope.
- `ContextAssembled` is legal in `READY` (the only state in which the drive loop proposes) and
  projects to `RunState.last_context_items`, so replay reproduces the exact context artifact.
- `BasicContextBuilder` assigns trust by origin, never by content: operator objective →
  `AUTHORIZED_HUMAN`, runtime plan → `RUNTIME_POLICY`, journaled tool/verifier feedback →
  `DETERMINISTIC_OBSERVATION`. A test feeds prompt-injection-shaped strings through every section
  to pin that classification.
- The `ModelPort` signature change rippled to `ScriptedModel`, the CLI, and both integration
  suites; all callers updated.

### Post-cycle hardening pass (2026-08-25, operator-initiated)

A dedicated hardening/debug/edge-test pass on the PACS-006 diff found and fixed three defects,
now pinned by `tests/regression/test_hardening_regressions.py`:

- `ContextItemSnapshot` did not re-validate item invariants — a decoded payload could carry
  naive datetimes, empty content, self-supersession, or a trust/provenance mismatch into durable
  replay state. Snapshots now mirror `ContextItem` validation (defense at the serialization
  boundary).
- `ModelContext` accepted supersession cycles (A supersedes B, B supersedes A), silently
  deactivating every item in the cycle. Cycles are now rejected at construction.
- `BasicContextBuilder` labeled a verification summary with unknown pass/fail as "failed" in
  provenance detail, fabricating a verifier outcome; it now labels it "unknown".

## Check

Executed in this environment (2026-08-25, uv-managed toolchain; numbers include the
post-cycle hardening pass):

- `uv run pytest -q` — **805 passing, 9 skipped** (skips are pre-existing macOS `RLIMIT_AS`
  platform limits, individually reason-coded)
- branch-aware coverage — **96.31% overall**; configured 90% gate satisfied;
  `domain/context.py`, `ports/context.py`, `adapters/context.py`, and the `ContextAssembled`
  codec paths all at 100% branch coverage
- `ruff format --check .` — clean (85 files)
- `ruff check .` — clean (0 findings)
- `pyright` (strict mode) — 0 errors, 0 warnings
- `lint-imports` — 2 architecture contracts kept, 0 broken
- deterministic CLI demo — `status=succeeded`, with `ContextAssembled` persisted per model turn
- `compileall` — clean

Acceptance-gate evidence, criterion by criterion:

- *context objects are typed and immutable at the model boundary* — `ModelPort.propose_action`
  takes `ModelContext`; the runtime rejects non-`ModelContext` builder output with
  `ContextContractError` (`test_runtime_rejects_non_context_from_builder`); frozen-instance
  mutation probes pass (`test_context_objects_are_immutable`). Fail-closed secret handling:
  a builder yielding SECRET-sensitivity context raises before any model call
  (`test_secret_rejection_happens_before_any_model_call`).
- *provenance/trust survive serialization/replay where required* — codec round-trip examples for
  `ContextAssembled` including supersedes/expires_at variants
  (`test_round_trip_preserves_every_event_type`); replay reproduces
  `state.last_context_items` (`test_runtime_passes_context_artifact_to_model_and_persists_it`);
  SQLite-backed integration suites persist and replay the new event.
- *untrusted content cannot independently authorize a privileged action* — code-owned tool
  metadata/permission checks unchanged and still pinned; new elevation guard matrix
  (`test_untrusted_and_model_content_can_never_gain_authority`, 4 denied combinations) plus
  allow-side tests (`test_promotion_with_basis_records_provenance`,
  `test_demotion_always_succeeds`).
- *deterministic tests cover authority ordering, supersession, and invalid elevation attempts* —
  `test_trust_authority_orders_every_class_strictly`,
  `test_active_items_exclude_superseded_and_expired`, supersession chain/cycle suites, Hypothesis
  freshness property, promotion deny matrix above plus the full
  `test_model_inference_elevation_targets_pinned` target sweep.

Hardening-pass edge coverage additionally includes: codec decode rejection for every malformed
`ContextAssembled` shape (non-array items, non-object snapshots, mistyped nullable fields,
naive/malformed datetimes, unknown enum values, SECRET payloads, trust/origin mismatch),
transition-legality matrix entries for `ContextAssembled` in every non-READY status, and replay
provenance survival.

## Stop

**SUCCESS.** All four acceptance-gate criteria are demonstrated by executable evidence; every
available quality gate is green.

## Follow-on implications

- PACS-007 can build `ContextBuilder.build(state, role, token_budget)`, selection, compaction, and
  versioned prompt artifacts on top of `ContextBuilderPort` / `ModelContext` without redefining
  trust or provenance.
- PACS-008 telemetry must treat `ContextAssembled` snapshots as the redaction boundary;
  `DataSensitivity` is already attached per item and SECRET content is already excluded from
  persistence.
- PACS-011 live adapters receive `ModelContext`, never `RunState`; secret isolation for provider
  credentials remains that cycle's responsibility.
