# PACS-016: Locked benchmark and multi-trial evaluation laboratory

Status: CLOSED (2026-09-03)

## Objective

Evaluate runtime policy scientifically — no judging success from demos or
one-off model runs. A locked, versioned benchmark fixture set; a
task/trial/grader/transcript model; a multi-trial live-model eval runner;
deterministic graders with a first-class false-success metric;
trajectory-quality metrics derived from the authoritative event stream;
success/cost/latency/human-intervention Pareto reports comparing runtime
configurations (not model brands); and console exposure of benchmark/trial
results (standing UI gate).

## Operator decisions (2026-09-03)

1. **Exploration-vs-no-progress tuning (PACS-015 deferred item 3) is FOLDED
   IN** — signed off at planning; it lands with laboratory A/B evidence
   (M8) instead of intuition.
2. **Live validation model: Ollama only** (`devstral-small-2:latest` +
   Docker), matching PACS-011..015 precedent; zero credentials in CI.
3. **Live matrix: focused subset** — 3 repair-category tasks × 2 runtime
   configurations × 2 trials live; fault-injection categories (provider
   outage, transient API) proven deterministically with wrapped adapters.

## Milestones

- **M1** `8be4219` — `domain/benchmarks.py`: closed vocabulary (12
  categories, sandbox modes, 4 grader ids, PASS/FAIL/**FALSE_SUCCESS**
  verdicts), task/trial/report dataclasses with strict validation, and
  `suite_lock_hash` — `BenchmarkSuite` refuses to exist when its stored
  hash disagrees with its content. 109 allow+deny tests; literal pinned
  sha256.
- **M2** `eb76cde` — `workloads/benchmarks.py`: the locked fixture set, one
  per category. Simple bug; multi-file; misleading failure (the failing
  test implicates `widget.py`, the defect lives in `util.py`); transient
  API (environment fault via descriptor); ambiguous success (a naive patch
  passes the visible tests but fails a code-owned edge-case hook — pinned
  STALLED, never verifier-granted); context pollution (264 KB of
  irrelevant docs); stale state (artifacts that disagree with reality);
  stall (subtle boundary bug); prompt injection (adversarial content,
  CONTAINER-only, solution never touches tests); HITL (approval-gated
  writes); parallel work (two-worker orchestrated, merge E2E); provider
  outage. Every solvable fixture pinned solvable end-to-end; every base
  pinned failing. `benchmark_content_lock()` extends the lock to fixture
  bytes; literal pinned.
- **M3** `6034e6e` — `application/graders.py`: deterministic graders (layer
  clean: domain/ports only — fixture knowledge arrives as `GraderEvidence`
  data). VERIFIED_SUCCESS (verifier-granted only), SCOPE_DISCIPLINE
  (PatchConstraints semantics), GROUND_TRUTH (tests byte-identical,
  known-naive patch rejected, hook evidence durable), RECOVERY
  (category-aware: graceful outage / transient recovery / bounded stall).
  Trial algebra pinned: success = SUCCESS_VERIFIED + all PASS;
  false success = SUCCESS_VERIFIED + any non-PASS. Review correction: a
  genuine solve on a stall task is PASS, never a false success.
- **M4** `2ef9308` — `application/trajectory.py`: the six trajectory
  metrics as honest stream projections — repetition (exact duplicate
  proposal signatures), expensive-model turns (caller-supplied tier set),
  scope violations, permission requests, context tokens (peak of ledger
  vs billed), recovery events — plus human-intervention counts.
  Provenance reuse evaluated and rejected for counts (recorded in the
  module docstring). Real-stream pins incl. the naive run's natural 0.75
  repetition ratio.
- **M5** `168e5de` — `adapters/fault_models.py` (deterministic counted
  TransientFailureModel / OutageModel raising exactly the runtime's
  transient shape) + `application/eval_runner.py` (layer-clean runner over
  an injected TrialDriver; deterministic trial ids; per-(config, task)
  ConfigReports; Pareto frontier: >= success, <= false-success/cost/
  latency/interventions with one strict; ties never dominate). M1
  uniqueness relaxed to (config, task) pairs — multi-task reports were
  impossible otherwise.
- **M6** `6b653fa` — `entrypoints/eval.py` (EvalTrialDriver: fresh
  workspace per trial, fault wrapping, HITL auto-grant as the counted
  intervention, evidence collection, platform preflight failing
  trusted-local trials fast with the true cause on RLIMIT_AS-rejecting
  hosts), `EvalReportStore` (versioned JSON, atomic writes, domain
  revalidation on load — tampered files fail loudly), code-owned presets,
  and the `loopforge eval` CLI (scripted default; live always explicit).
- **M7** `e0b0dbe` — REST (`/api/benchmark/suite` read-only locked
  definition + both hashes; `/api/evals` list; `/api/evals/{id}` detail;
  unknown → 404, tampered → 500) and console (hash-routed Evals list +
  detail, false-success cells red, Pareto rows highlighted, both lock
  hashes visible). Screenshots: `output/playwright/m7-*.png`.
- **M8** `21a9d96` — exploration-vs-no-progress tuning (folded-in deferred
  item; ADR-0013): `Runtime.verify_read_only_turns=False` skips
  verification after successful READ-only turns (code-owned metadata is
  the authority, rule 4); reducer progress semantics untouched (one
  additive transition — pre-016 streams replay byte-identical); legacy
  cadence selectable for A/B. Laboratory evidence: scripted exploring
  model on bench-simple-bug/bench-stall — baseline 2/2 success both
  tasks, legacy-progress 0/2 (every trial STALLED before the fix landed);
  flailing guards pinned unharmed in both modes. Also fixed a
  pre-existing PACS-012 test that segfaulted Linux runs (negative rlimit
  is RLIM_INFINITY there).
- **M9** `2992320` — dedicated adversarial review (two independent
  reviewers; 16 confirmed findings, all fixed and pinned). HIGH: the
  content lock pinned hook identity, not hook CODE — neutralizing the
  ambiguous-success edge check left the lock unchanged; the lock now
  hashes `inspect.getsource` per check (new literal
  `9bc7fe19125b4946c2f1e10301b0e1b7de77a31b2ce81b2635bf57c00e969924`).
  MEDIUMs: hook-marker forgery via crafted filenames in failed
  verification summaries (marker check now restricted to passing
  summaries; required check names derived from code-owned hooks via a
  SandboxPort probe, never scraped from summaries); unguarded evidence
  reads (model-planted symlink/binary/oversize files could hang/OOM the
  driver — now symlink-rejecting, 1 MiB-capped, UTF-8-validated, loud
  EvalTrialError); context metrics could under-report (now max of ledger
  and billed peaks). LOWs: ConfigReport sum invariant, transient
  count-vs-streak pin, runner contract hardening, store tmp-sweep race,
  unsafe report id → 404 without path leak, VERIFYING crash/resume pin,
  no_progress_limit envelope narrowing, stale-lock console badge, mkdtemp
  fallback cleanup, CLI internal-leak guard, subset-report task labeling.
- **M10** (below) — live validation.
- **M11** (this record) — ADR-0012, ADR-0013, cycle record, HANDOFF
  checkpoint.

## Live validation (2026-09-03; Ollama `devstral-small-2:latest` + Docker `python:3.12-alpine`)

`tests/live/test_benchmark_eval_live.py` (skip-gated on the Ollama/Docker
probe precedent) drove the production `EvalTrialDriver` through
`run_trials` on the operator-approved focused matrix: bench-simple-bug,
bench-misleading-failure, bench-stall × baseline and tight-budget × 2
trials — 12 live container trials, 216 s wall clock, zero credentials.

| config | task | trials | success | false-success | mean latency |
|---|---|---|---|---|---|
| baseline | bench-simple-bug | 2 | 2 | 0 | 16.99s |
| baseline | bench-misleading-failure | 2 | 2 | 0 | 24.81s |
| baseline | bench-stall | 2 | 2 | 0 | 25.84s |
| tight-budget | bench-simple-bug | 2 | 2 | 0 | 12.87s |
| tight-budget | bench-misleading-failure | 2 | **0** | 0 | 10.98s |
| tight-budget | bench-stall | 2 | 2 | 0 | 15.67s |

- **The laboratory's first genuine configuration finding:** tight-budget
  (no_progress_limit=2) failed both misleading-failure trials while
  baseline solved both — the tighter no-progress limit kills the
  wrong-path recovery the misdirection demands. Exactly the
  runtime-configuration insight (not a model-brand claim) this cycle
  exists to produce.
- **Zero false successes**, asserted as a hard invariant: for these
  tasks' bound graders a false success requires a verifier/grader
  divergence — a harness defect, not model nondeterminism.
- **Both configs on the Pareto frontier** (baseline better on success,
  tight-budget faster — neither dominates): correct M5 dominance
  semantics with real nondeterministic data.
- Structural invariants held on all 12 trials: terminal stops, full
  grader coverage, report validates and round-trips the store.
- Report persisted and served over REST; console evidence:
  `output/playwright/m10-evals-live.png`,
  `output/playwright/m10-eval-detail-live.png` (suite lock matches the
  live suite — no stale badge).
- Evals use in-memory/SQLite stores only — production Postgres
  untouched; the PG integration suite remains `loopforge_test`-only
  (PACS-015 M1 guard unchanged).

## Acceptance gates

- Benchmarks defined independently of the policy under test ✓ (code-owned
  fixtures/graders; content lock pinned; ADR-0012; policy knobs narrow
  only, never widen — M9 W6 pin)
- Multiple trials produce aggregate nondeterministic metrics ✓ (M5
  runner; M10 live matrix)
- False-success rate is first-class ✓ (M1 verdict, M3 algebra, console
  red cells, M10 hard invariant)
- Deterministic tests and live-model evals remain separate ✓
  (`tests/live/` skip-gated; `--ignore=tests/live` zero-credential green
  at every milestone)
- Reports compare runtime configurations, not model brands ✓
  (EvalConfiguration presets; Pareto frontier over configs)
- Console exposes benchmark/trial results ✓ (M7 + M9 stale-lock badge;
  screenshots)
- Test-count progression: 1709 → 2074 passed / 56 platform-gated skips
  (full suite — both live tests executed: the PACS-011 repair E2E and
  the M10 matrix; 2072 passed / 56 skipped with `--ignore=tests/live`,
  zero credentials); branch coverage ≥
  90 floor at every milestone; ruff format+check, pyright strict,
  lint-imports, UI tsc+vite green at every milestone.

## Deferred

- Report curation API (retiring obsolete reports) — filesystem-only by
  design (ADR-0012 consequences).
- Per-turn latency from telemetry spans (wall-clock from events suffices
  for the Pareto axis).
- Live runs of the HITL/parallel/prompt-injection categories (proven
  deterministically; a larger live matrix is an operator choice).
