# PACS-007 — Context lifecycle and prompt contracts

Status: SUCCESS

## Objective

Build deterministic context selection, token budgeting, compaction/pruning, and versioned prompt
artifacts on top of the PACS-006 context authority model.

## Why now

PACS-006 made model context a typed, provenance-aware boundary artifact, but the builder emitted
an unbounded superset of state with no selection policy: anything the projection held reached the
model. Every later cycle that touches models (PACS-008 observability, PACS-011 live adapters,
PACS-017 adaptive context) needs context construction to be a deterministic, budgeted,
explainable policy function with versioned prompt contracts first.

## Dependencies

- PACS-006 (SUCCESS). Trust authority (`TRUST_AUTHORITY`, guarded `promote`, SECRET persistence
  rejection) is reused unchanged; this cycle adds no elevation path.

## In scope

- `ContextBuilder.build(state, role, token_budget)` — the port now takes a `ModelRole` and an
  optional `ContextTokenBudget`;
- explicit context-budget accounting (`ContextAccounting` / `AccountingEntry` ledger, including
  template-overhead charging; over-budget ledgers are unrepresentable);
- preservation contracts (`PreservationClass`) for objective, blockers, latest verifier failures,
  externally confirmed facts, pending approvals, and irreversible actions;
- structured compaction/pruning (`select_context`, `truncate_content` with a fixed marker,
  supersession/freshness pruning, dangling-edge repair);
- role-specific context assembly (`ModelRole`: CONTROLLER / PLANNER / REFLECTOR);
- versioned prompt templates/artifacts (`PromptTemplate`, `PromptSection`, `RenderedPrompt`,
  `default_controller_template`) with template id/version captured on the durable
  `ContextAssembled` event;
- stable-prefix layout: stable template sections are a construction-enforced prefix, cache-friendly
  without encoding any provider's caching semantics;
- snapshot/invariant tests for context construction, including repeated-compaction survival and a
  Hypothesis budget/preservation property.

## Out of scope

- provider-specific prompt-cache implementation, live models (per cycle spec);
- observability/telemetry (PACS-008) — the builder's `last_accounting` ledger is the seam PACS-008
  will emit from, but no telemetry pipeline is built here;
- adaptive budget allocation (PACS-017) — budgets are static policy inputs here.

## Plan

1. Domain lifecycle module (`domain/context_lifecycle.py`): preservation/drop vocabularies,
   token budget, candidate model, accounting ledger, truncation, and the deterministic
   `select_context` policy.
2. Prompt module (`domain/prompts.py`): versioned templates, stable-prefix invariant, rendering,
   default controller template.
3. Vocabulary additions to `domain/context.py`: `ModelRole`, `PromptTemplateRef`, and
   `ModelContext.role` / `ModelContext.prompt_template` (both defaulted; PACS-006 invariants
   untouched).
4. Port extension (`ports/context.py`): role + optional budget on `ContextBuilderPort`;
   `TokenCounterPort` for provider-independent counting.
5. Adapters (`adapters/context.py`): `CharsPerTokenCounter`, `BudgetedContextBuilder`;
   `BasicContextBuilder` signature alignment (budget explicitly out of its contract).
6. Execution metadata: `ContextAssembled` gains optional prompt template id/version (codec
   backward-compatible: legacy payloads decode to null); runtime persists them per model turn.
7. CLI demo rewired to `BudgetedContextBuilder`.
8. Deterministic tests: selection/compaction/preservation invariants, role assembly, template
   rendering snapshots, codec round-trip, runtime integration.
9. Reconcile roadmap/build-status/handoff documentation.

## Act

Implemented as planned. Notable decisions and discoveries:

- **Selection lives in the domain, estimation behind a port.** `select_context` is a pure,
  deterministic policy function over candidates; token counting is injected as
  `Callable[[str], int]` and exposed to adapters as `TokenCounterPort`. The reference counter is a
  documented character heuristic (`CharsPerTokenCounter`, 4 chars/token), explicitly not any
  provider's tokenizer.
- **Fixed processing order.** Role scoping → supersession/freshness → preserved allocation →
  ranked greedy fill. Preservation contracts apply to live, role-eligible candidates; dead
  (superseded/expired) content is pruned before budgeting and recorded with a reason code.
- **Preserved content is all-or-nothing.** Preserved candidates are allocated first, in input
  order, and are never truncated or dropped; if they exceed the usable budget the build raises
  `ContextBudgetError` — "fits the budget or fails explicitly" with no silent degradation.
- **Compaction may drop content, never elevate it.** Truncation produces a new item via
  `dataclasses.replace` touching only `content` (prefix + fixed `\n…[truncated]` marker, longest
  fitting prefix found by deterministic binary search); trust, provenance, sensitivity, and
  timestamps are carried over verbatim. Tests pin that model-inference content stays
  model-inference after compaction. Non-compactible candidates are dropped whole with
  `DropReason.OVER_BUDGET` rather than truncated.
- **Dangling supersession edges are pruned.** Selection drops superseded items before budgeting,
  so a kept successor's `supersedes` edge would otherwise dangle and fail `ModelContext`
  construction; edges to dropped items are cleared as pure bookkeeping (content/trust/provenance
  untouched) and the result is always a legal `ModelContext` payload.
- **Template overhead is charged against the budget.** `BudgetedContextBuilder` counts the
  template's static text as `overhead_tokens`, so the rendered prompt and its items together fit
  the budget; if overhead plus reserve consumes everything, the build fails explicitly.
  `ContextAccounting` rejects over-budget ledgers at construction, making an over-budget assembly
  unrepresentable.
- **Rank order is trust-first, then recency, then id.** Under pressure the lowest-trust content
  (untrusted content, model inference) is sacrificed first; ties break by `created_at` then
  `item_id`, fully deterministic.
- **Backward-compatible event metadata.** `ContextAssembled` gained optional
  `prompt_template_id` / `prompt_template_version` (validated: recorded together, non-empty).
  Schema stays v1; legacy payloads decode to null, new payloads round-trip through the canonical
  fixed-point test unchanged.
- **Role scoping in the reference builder.** Observations are controller/reflector-only,
  reflections controller/planner-only, plans controller/planner-only; preserved classes are
  visible to all roles so role scoping can never silently strip a required fact.
- **`BasicContextBuilder` is retained** for unbudgeted wiring (existing tests, boundary contract
  probes) and now records the role; it explicitly ignores `token_budget` — budgeting is
  `BudgetedContextBuilder`'s contract.

### Post-cycle hardening pass (2026-08-27, operator-initiated)

A dedicated hardening/debug/edge-test pass on the PACS-007 diff found and fixed four defects,
all pinned by `tests/regression/test_hardening_regressions.py`:

- The runtime accepted a `ModelContext` assembled for a **different run**, persisting another
  run's context items under this run's `ContextAssembled` event and feeding cross-run context to
  the model. The boundary now rejects a run-id mismatch with `ContextContractError` before
  persistence and before any model call (`test_runtime_rejects_context_assembled_for_a_different_run`).
- A misbehaving token counter returning **negative counts** flowed into budgeting arithmetic and
  only failed later, deep inside ledger validation. Counts are now validated at the measurement
  point (`test_selection_rejects_negative_token_counts`,
  `test_selection_rejects_negative_counts_for_compacted_content`).
- A failed build left the previous successful build's accounting ledger in place, so a failed
  turn would be misattributed the stale ledger by later telemetry. A failed build now clears the
  ledger (`test_failed_build_clears_stale_accounting`).
- `truncate_content("", allowance=0)` returned `None` even though empty content fits a zero
  allowance, violating the documented fits-returns-original contract
  (`test_truncate_returns_empty_content_that_fits_zero_allowance`).

The pass also pinned edge behavior so it cannot drift: preservation precedence over the
compactible flag, deterministic SUPERSEDED-over-EXPIRED reason precedence, supersession-cycle
pruning, role-scoped supersession evaluation, exact-fit boundaries, ModelContext legality of
arbitrary selection output (also asserted inside the Hypothesis property), reversible-metadata
handling, missing-version-key codec rejection, immutability of the new execution-metadata fields,
stable-only template rendering, and counter granularity validation. A SQLite integration test
(`test_budgeted_context_metadata_survives_sqlite_persistence_and_replay`) proves template
metadata survives durable persistence and replay.

## Check

Executed in this environment (2026-08-27, uv-managed toolchain; numbers include the
post-cycle hardening pass):

- `uv run pytest -q` — **897 passing, 9 skipped** (skips are pre-existing macOS `RLIMIT_AS`
  platform limits, individually reason-coded)
- branch-aware coverage — **97% overall**; configured 90% gate satisfied;
  `domain/context.py`, `domain/context_lifecycle.py`, `domain/prompts.py`,
  `adapters/context.py`, `ports/context.py`, `domain/events.py`, and `adapters/json_events.py`
  all at 100% branch coverage
- `ruff format --check .` — clean (90 files)
- `ruff check .` — clean (0 findings)
- `pyright` (strict mode) — 0 errors, 0 warnings
- `lint-imports` — 2 architecture contracts kept, 0 broken
- deterministic CLI demo — `status=succeeded`, now wired through `BudgetedContextBuilder` with
  the versioned `loopforge.controller/1.0.0` template
- `compileall` — clean

Acceptance-gate evidence, criterion by criterion:

- *model context deterministically fits the requested budget or fails explicitly* — accounting
  invariants make over-budget ledgers unconstructable
  (`test_accounting_rejects_over_budget_ledgers`); preserved overflow raises
  `ContextBudgetError` (`test_preserved_overflow_fails_explicitly`,
  `test_budgeted_builder_fails_explicitly_when_preserved_exceeds_budget`); overhead/reserve
  exhaustion fails explicitly (`test_overhead_and_reserve_consuming_budget_fail_explicitly`,
  `test_budgeted_builder_fails_when_template_overhead_consumes_budget`); a Hypothesis property
  sweeps budgets and preservation masks asserting fit-or-explicit-failure
  (`test_property_selection_fits_budget_or_fails_and_never_drops_preserved`).
- *compaction never drops required preserved facts* — preserved allocation precedes all
  compaction and is whole-only (`test_preserved_candidates_are_kept_whole_under_pressure`); all
  six preservation classes are pinned
  (`test_preservation_classes_pin_the_six_required_contracts`) and mapped from state
  (`test_budgeted_builder_records_preservation_classes_in_accounting`,
  `test_budgeted_builder_marks_passed_verification_as_confirmed_fact`,
  `test_budgeted_builder_marks_pending_approvals`,
  `test_budgeted_builder_marks_irreversible_actions`); truncation never alters trust/provenance
  (`test_compaction_never_alters_trust_or_provenance`).
- *prompt/template versions are captured as execution metadata* — the runtime records template
  id/version on every durable `ContextAssembled` event
  (`test_runtime_records_prompt_template_metadata_with_budgeted_builder`), null when no template
  is used (`test_runtime_records_null_template_metadata_without_template`); codec round-trip and
  legacy-payload compatibility are pinned
  (`test_context_assembled_round_trip_with_prompt_template_metadata`,
  `test_context_assembled_legacy_payload_without_template_metadata_decodes`).
- *equivalent state + policy yields equivalent context construction* — selection determinism
  (`test_selection_is_deterministic_for_equivalent_inputs`), builder determinism with a fixed
  clock (`test_budgeted_builder_is_deterministic_and_records_template_ref`), byte-identical
  prompt rendering (`test_render_is_byte_identical_for_equivalent_inputs`), and a golden
  render snapshot (`test_default_template_render_snapshot`).
- *tests prove high-signal context survives repeated compaction* — five rounds of shrinking
  budgets fed through their own survivors keep the preserved objective whole
  (`test_high_signal_context_survives_repeated_compaction`); low-trust content is sacrificed
  first (`test_lowest_trust_is_dropped_first_under_pressure`).

## Stop

**SUCCESS.** All five acceptance-gate criteria are demonstrated by executable evidence; every
available quality gate is green. PACS-006 trust authority is intact: no new elevation path was
added, compaction touches content only, and SECRET persistence rejection is unchanged.

## Follow-on implications

- PACS-008 can emit context-build spans/metrics from `BudgetedContextBuilder.last_accounting`
  and the `ContextAssembled` template metadata without new plumbing; `DataSensitivity` remains
  the redaction boundary.
- PACS-011 live adapters receive budgeted `ModelContext` plus a versioned `RenderedPrompt`;
  provider tokenizers/caching stay quarantined behind `TokenCounterPort`-style adapters.
- PACS-013 worker roles can reuse `ModelRole` scoping or extend the vocabulary with new roles
  without changing selection semantics.
- PACS-017 adaptive context allocation may tune `ContextTokenBudget` and compaction inputs as
  policy, but preservation classes and trust authority remain immutable during runs.
