# PACS-017: Adaptive context and shadow policies / v1.0

Status: CLOSED (2026-09-07)

## Objective

Demonstrate bounded adaptation from execution history while preserving
immutable runtime authority: adaptive context-budget allocation,
versioned candidate execution policies, shadow-policy evaluation (advice
journaled, never enacted), counterfactual replay, policy comparison on
the locked suite, an operator-owned evidence-gated promotion workflow,
and bounded statistical derivation heuristics — closing v1.0.

Thesis under test:

> AI contributes intelligence; engineered systems provide reliability.
> Adaptive execution may optimize within authority, but it cannot expand
> its authority.

## Operator decisions

1. **Immutable surfaces are unrepresentable, not runtime-denied.** The
   `ExecutionPolicy` vocabulary (ADR-0014) carries only adaptive knobs —
   routing, context allocation, verification cadence, worker-count
   preference. Permissions, security boundaries, legal transitions, hard
   budgets, HITL, secrets, and authority-expansion rules have no fields
   (AGENTS.md rules 11/12).
2. **Shadow never enacts — pinned byte-identical.** The candidate
   advises at the three adaptive decision points; the active path with
   and without a shadow is byte-identical (M3 pin), shadow failure is
   honest absence, and the advisor's only view of the run is a read-only
   accounting snapshot.
3. **Promotion is operator-owned, evidence-gated, terminal.** CANDIDATE →
   PROMOTED is illegal; PROMOTED/RETIRED are terminal; supersession is
   register-plus-promote of a new version (an early PROMOTED-uniqueness
   check was removed during M6 for deadlocking supersession). Rule 16:
   every transition carries a referenced evidence basis; promotion needs
   the literal confirm token.
4. **Heuristics are suggestions through the domain gate.** M8 derivation
   is deterministic, clamped into code-owned envelopes, constructed
   through `ExecutionPolicy` validation, and registers as CANDIDATE —
   never applied, never promoted.
5. **Live validation: Ollama only** (`devstral-small-2:latest` + Docker
   `python:3.12-alpine`), matching precedent; zero credentials in CI.

## Milestones

- **M1** `a836821` — `domain/policies.py`: the `ExecutionPolicy`
  vocabulary (routing knobs, context-allocation bounds, cadence, worker
  count) with closed validation; bool-rejecting numeric checks; built-in
  `baseline`/`adaptive-context`/`patient-router` policies. 65 tests.
- **M2** `d88d1d7` — `AdaptiveContextBuilder`: per-turn budget moves only
  inside policy bounds (grow on over-budget drop, contract on low
  utilization), delegate contracts pinned unharmed;
  `repair_context_builder` fails closed on envelope widening. 19 tests.
- **M3** `c334bba` — shadow seam: `ShadowDecisionRecorded` (catalog
  25→26, closed kinds: model_route/context_budget/verification_cadence,
  legal only in READY/VERIFYING), `CandidateShadowAdvisor`,
  `Runtime.shadow`, the byte-identity pin, shadow-failure honest absence.
- **M4** `42fb350` — counterfactual replay: `application/counterfactual.py`
  + `entrypoints/replay.py` + CLI `replay` — deterministic re-drive from
  a stream prefix, closed outcome vocabulary, fake-MATCHED unrepresentable.
  34 tests.
- **M5** `c0018d0` — policy comparison via the eval lab: report schema
  v1→v2 (+context/recovery means with a v1 shim), `EvalConfiguration`
  policy knobs resolved per trial fail-closed. 31 tests.
- **M6** `8c582a8` — controlled promotion: `PolicyLifecycle` closed
  table, `PolicyRecord` (mandatory evidence basis), `PolicyRegistryStore`
  (operator-owned, atomic, exact-key, revalidating), CLI
  `policy --list/--show/--promote`, REST reads + confirm-gated promote
  (StrictBool). 79 tests.
- **M7** `f672860` — console exposure: policy registry/detail views with
  checkbox-gated promote, session shadow-decisions panel ("nearest
  enacted (heuristic)"), eval v2 axes columns, evidence links;
  `serve --policies-dir` wiring gap closed. Screenshots verified.
- **M8** `a8cee3f` — bounded statistical heuristics:
  `derive_candidate_policy` (p90 context ceiling in a code-owned
  envelope, recovery-weighted stall threshold, basis truncation),
  `shadow_budget_samples`, CLI `policy --derive`. 26 tests.
- **M9** `664fad5` — adversarial review (two parallel reviewers; core
  invariants confirmed defended) and per-finding fixes+pins: CLI
  `--transition` making every evidence state reachable (A1), shadow
  wired through the real composition roots (A2, A10), thread-safe
  pid+uuid tmp names (A3), filename↔content binding (A6), OSError
  taxonomy + path-leak-free 500s (B1/B2), typed schema markers (B6),
  bool-rejecting float validators (B7), typed `updated_at` (B8),
  `--sqlite` never silently created (A11/B3), v1 zero-fill excluded
  from the recovery mean (B4), busy-guard + explicit version in the
  console promote (B10/A5); doc-only dispositions recorded in the store
  docstrings. 38 pin tests.
- **M10** `446d871` — live validation (below).
- **M11** — v1.0 artifacts: ADR-0014, benchmark-methodology and
  honest-limitations docs, architecture overview update, this cycle
  record, HANDOFF checkpoint, v1.0 checklist verification.

## Live validation (2026-09-07; Ollama `devstral-small-2:latest` + Docker `python:3.12-alpine`)

`tests/live/test_policy_adaptive_live.py` (skip-gated on the Ollama/Docker
probes) drove two validations:

**Policy matrix** — bench-simple-bug, bench-misleading-failure,
bench-stall × `policy-baseline` and `policy-adaptive-context` × 2 trials
= 12 live container trials through the production `EvalTrialDriver` with
the M5 per-trial `_policy_for` wiring (187 s wall clock, zero
credentials):

| config | task | trials | success | false-success | latency | ctx-tok | recovery |
|---|---|---|---|---|---|---|---|
| policy-adaptive-context | bench-simple-bug | 2 | 2 | 0 | 13.25s | 953 | 0.00 |
| policy-adaptive-context | bench-misleading-failure | 2 | 2 | 0 | 17.05s | 1124 | 2.00 |
| policy-adaptive-context | bench-stall | 2 | 2 | 0 | 15.64s | 954 | 0.00 |
| policy-baseline | bench-simple-bug | 2 | 2 | 0 | 12.96s | 953 | 0.00 |
| policy-baseline | bench-misleading-failure | 2 | 2 | 0 | 17.97s | 1124 | 2.00 |
| policy-baseline | bench-stall | 2 | 2 | 0 | 15.44s | 954 | 0.00 |

- **Zero false successes** asserted as a hard invariant; v2 axes measured
  (not zero-filled) on every row — the context axis feeds the M8
  heuristics, the recovery axis the stall knob.
- **Pareto frontier: `policy-adaptive-context`** — the v1.0 gate's
  "quality, cost, latency, context, recovery, and human-intervention
  tradeoffs across at least two execution policies" demonstrated on the
  locked suite. Honest note: the focused 3-task matrix did not
  differentiate the arms on outcomes; the frontier reflects the tie-broken
  dominance semantics, and the report says exactly that.
- Report `live-pacs017-m10-policy-matrix` (suite lock `e97e6454306e`)
  persisted, served over REST, and screenshotted in the console.

**Live shadowed run** — `adaptive-context` v1 shadowed through the
container composition root (`deps.shadow_policy`, M9 A2 wiring):
`run_20479e430f01` succeeded via the verifier stack with **6
`ShadowDecisionRecorded` events** journaled across all three kinds
(model_route, context_budget, verification_cadence) — evidence only;
the active repair landed byte-identical to the unshadowed fixture
solution.

Console evidence: `output/playwright/m10-evals.png`,
`m10-eval-detail.png` (v2 axes + pareto badges),
`m10-session-shadow.png` (shadow panel on the live run),
`m10-policies.png`, `m10-policy-detail.png`.

## Acceptance gates

PACS-017 gate (remaining-pacs-plan):

- An adaptive candidate can be benchmarked and shadowed without changing
  active-run authority ✓ (M2/M3/M5 + M10 live matrix and shadowed run;
  byte-identity pin)
- Candidate policy cannot self-promote ✓ (no runtime transition path;
  registry has no register/delete REST surface; M9 reviewers confirmed)
- Counterfactual/shadow results auditable through provenance and
  telemetry ✓ (durable catalog-26 events, session shadow panel,
  provenance view; M4 closed outcome vocabulary)
- Deterministic safety policy immutable during execution ✓ (ADR-0014
  unrepresentable vocabulary; rules 11/12 pins)
- v1 benchmark/report demonstrates tradeoffs across ≥2 execution
  policies ✓ (M10 matrix, v2 axes, Pareto frontier)
- Documentation and release artifacts describe limits honestly ✓
  (`docs/architecture/honest-limitations.md`, benchmark-methodology.md,
  ADR-0014 consequences, store docstrings)
- Console exposes shadow-policy/candidate comparisons ✓ (M7 views +
  M10 live screenshots)

v1.0 completion standard (all earlier cycle gates remain green — full
suite 2406 passed / 57 platform-gated skips at M9, plus both M10 live
tests executed):

1. bounded/replayable event-sourced runtime ✓ (PACS-001/002)
2. persistent state with optimistic concurrency ✓ (PACS-002)
3. retries, idempotency, circuit breaking, budgets, cancellation, stall
   detection ✓ (PACS-003)
4. explicit security/tool/sandbox capability contracts ✓ (PACS-005)
5. provenance-aware context and deterministic compaction ✓ (PACS-006/007)
6. structured telemetry ✓ (PACS-008)
7. isolated software-repair workload ✓ (PACS-009/010)
8. live provider support without provider-owned control flow ✓ (PACS-011)
9. routing and bounded orchestration ✓ (PACS-012/013)
10. independent verification, Reflexion artifacts, durable HITL ✓
    (PACS-010/014)
11. execution provenance graph ✓ (PACS-015)
12. locked multi-trial benchmark/eval laboratory ✓ (PACS-016)
13. shadow/adaptive execution policy that cannot modify immutable
    authority ✓ (this cycle, ADR-0014)
14. documented architecture, ADRs, threat model, benchmark methodology,
    and limitations ✓ (M11 docs; ADR-0001..0014)
15. reproducible developer checks and a clean release build ✓ (ruff
    format+check, pyright strict, 2 import-linter contracts, UI
    tsc+vite, full pytest with branch coverage ≥ 90 — green at every
    milestone; live suites skip-gated with reason codes)

## Deferred

- Authenticity binding for operator stores (signed artifacts) —
  trusted-local threat model documented (D10; honest-limitations).
- flock/CAS multi-writer registry — single-operator-writer documented.
- Shadow samples pooled across policy ids on readback — evidence, not
  authority (ADR-0014 consequences).
- Learned model routing, semantic progress detection, candidate strategy
  extraction — post-v1 candidates per the master build map.
