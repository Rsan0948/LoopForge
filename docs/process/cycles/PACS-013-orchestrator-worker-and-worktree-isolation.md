# PACS-013 — Orchestrator/worker and worktree isolation

Status: SUCCESS

## Objective

Add bounded multi-agent execution only where decomposition provides measurable
value: an orchestrator owns the global plan and a shared authoritative event
store, workers own assigned Git-worktree workspaces and per-worker run streams,
and a deterministic merge/reconciliation policy combines worker results —
without ceding any runtime authority (budgets, permissions, verification truth,
and stopping stay exactly where PACS-001–012 put them).

## Why now

PACS-010 made software repair the reference workload with Git-backed
workspaces; PACS-012 delivered reason-coded per-turn model routing whose
`RoutingSignals` are per-turn and whose `ModelRequirements` are role-shaped —
per-worker-role model selection is now a wiring-time decision, not a runtime
change. `WorkerId` (`domain/types.py`) and `CorrelationIds.worker_id`
(`domain/telemetry.py`) have been reserved for this cycle since PACS-008. Every
seam the orchestrator needs exists; what is missing is the worker lifecycle
vocabulary, the worktree isolation, the merge policy, and the orchestrator
itself.

## Dependencies

- PACS-010 (SUCCESS): `GitWorkspaceManager` (hermetic offline fixtures,
  deterministic base commits, materialize-time metadata fingerprint verified
  before every host-Git invocation), `RepairVerifier` (verifier truth is
  code-owned), `WorkspaceArtifactCollector` durable evidence.
- PACS-012 (SUCCESS): `ModelRegistry`/`TieredRoutingPolicy` per-turn routing
  seam consumed per worker; `RepairRuntimeBundle.close()` fan-out contract
  (multi-model teardown); model identity is telemetry-only by design — worker
  attribution follows the same projection discipline.
- PACS-008 (SUCCESS): `CorrelationIds.worker_id` reserved slot; closed
  telemetry vocabularies; fail-safe non-authoritative emission boundary.
- PACS-001–005 (SUCCESS): SQLite compare-and-append event store
  (`StreamVersionConflictError` on stale `expected_version` — the optimistic
  concurrency mechanism); `ControlPolicy`/`BudgetLimit` immutability precedent
  (rule 12: workers get shares, never new authority).

## Operator decisions (2026-08-28, scope confirmation before any code)

1. **Event catalog**: explicit sign-off to add a minimal worker lifecycle set
   to the frozen schema-v1 (19-event) catalog — exactly three new event types
   (`WorkerSpawned`, `WorkerStopped`, `WorkerMerged`), catalog grows to 22,
   `SCHEMA_VERSION` stays 1. All four touchpoints (Event union, reducer arm +
   `_ALLOWED_STATUS`, JSON codec registry/constructor/dispatch, telemetry
   projector arm) move together.
2. **Concurrency**: deterministic interleaved scheduling — single process, the
   orchestrator drives worker runtimes cycle-by-cycle in a fixed code-owned
   order. Logical concurrency (isolated state/workspaces), zero wall-clock
   races; replay stays trivial; deterministic CI preserved.
3. **Merge policy**: real Git merge of worker worktree branches into a
   code-owned integration repository in spawn order; a conflict aborts that
   merge, records `WorkerMerged(CONFLICT)` durably, and stops the orchestrated
   run `FAILURE` explicitly — never a silent overwrite.
4. **Budget split**: static shares at spawn — the orchestrator partitions the
   existing code-owned `BudgetLimit` into per-worker shares validated to never
   sum above the global limit; `ControlPolicy` remains the sole enforcer per
   worker stream; a worker exhausting its share stops that worker, not its
   siblings.

## In scope

- **Worker lifecycle events (3 new, catalog 19→22, schema v1)**:
  - `WorkerSpawned` (orchestrator stream): worker id, worker run id, assigned
    workspace id, assignment objective, cost-budget share — worker ownership is
    durable and replayable.
  - `WorkerStopped` (orchestrator stream): worker id, closed `WorkerOutcome`
    vocabulary (SUCCEEDED/FAILED/BUDGET_EXHAUSTED/CANCELLED), bounded summary.
  - `WorkerMerged` (orchestrator stream): worker id, closed `MergeOutcome`
    vocabulary (MERGED/CONFLICT), merge revision (when merged), bounded detail.
  - `RunState` gains a replayable worker roster projection
    (`workers: tuple[WorkerProjection, ...]`); the `WorkerMerged` reducer arm
    transitions the orchestrator run to `VERIFYING` exactly when every spawned
    worker is stopped and every succeeded worker has a merge outcome.
- **Orchestrator** (`application/orchestrator.py`, workload-agnostic like
  `Runtime`): owns the global plan (ordinary `PlanCreated` on its own stream),
  spawns a bounded number of workers (code-owned `max_workers`), drives them
  round-robin one cycle at a time, records worker terminal outcomes, merges
  succeeded workers in spawn order, verifies the merged workspace through a
  verifier bound to the integration workspace, records merged evidence through
  the existing `ArtifactRecorded` path, and stops with durable reason codes
  (`WORKER_MERGE_CONFLICT`, `WORKER_INCOMPLETE`, `MERGED_VERIFICATION_FAILED`,
  `ORCHESTRATOR_BUDGET_EXHAUSTED`) through the existing `RunStopped` event.
- **Runtime step seam** (`application/runtime.py`): the drive loop's per-cycle
  body is extracted so an orchestrator can advance a worker runtime exactly one
  cycle (`step(run_id)`); drive-loop locals (failure streak, active model,
  fallback flag) become per-run drive state so stepped and blocking drives are
  semantically identical. `resume()`/`run()` behavior unchanged.
- **Git worktree isolation** (`adapters/git_workspace.py` extension):
  one code-owned integration repository per orchestrated run (existing
  hardened `materialize` path); per-worker linked worktrees
  (`git worktree add <path> -b worker/<id> <base>`) so no two workers share a
  mutable filesystem workspace; worker commits and merges executed host-side
  with the same hermetic environment; the metadata-fingerprint defense
  extended to the linked-worktree layout (`.git` pointer file + shared gitdir
  metadata) so a mount-tampered pointer fails closed before any host-Git call.
- **Deterministic merge/reconciliation**: spawn-order merges
  (`git merge --no-ff --no-edit worker/<id>`); conflict → `git merge --abort`
  → `WorkerMerged(CONFLICT)` → `FAILURE` stop (`WORKER_MERGE_CONFLICT`).
- **Budget partitioning** (domain-owned `partition_budget`): static per-worker
  shares of the global `BudgetLimit` (cost/tokens additive and validated to
  sum ≤ global; iterations/elapsed per worker inherited from the global
  limit); per-worker `ControlPolicy(share)` enforcement unchanged; an
  orchestrator-level aggregate cost check as defense-in-depth.
- **Worker attribution telemetry** (projection discipline, PACS-012
  precedent): the three new events get metadata-only projector arms carrying
  `loopforge.worker_id`; `Runtime` gains an optional `worker_id` that threads
  through `RuntimeTelemetry` correlation (default `None` — single-runtime
  spans byte-identical); closed `MetricName` additions for worker lifecycle.
- **Per-worker routing and context** (wiring-time, no core change): each
  worker runtime gets the shared registry through its own router injection;
  worker context is scoped by the worker's own stream/objective through the
  existing `ContextBuilderPort` decorator pattern (`RepairContextBuilder`).
- **Benchmarkable multi-agent path** (entrypoints): a two-module decomposed
  repair fixture (independent buggy modules, disjoint per-worker patch
  constraints), an `orchestrated-repair-demo` CLI command wiring two scripted
  workers by default (`--container`/`--model ollama` variants), and per-worker
  sandbox/verifier/artifact wiring. The single-runtime `repair-demo` scripted
  path stays byte-identical — multi-agent is opt-in, never a default.
- **Tests**: event catalog pins 19→22; reducer/codec/telemetry arms for the
  new events; worktree adapter tests (isolation, tampered-pointer fail-closed,
  merge/conflict, cleanup) behind the existing git skipif; budget-partition
  unit tests; orchestrator integration tests with scripted workers
  (interleaved drive, merge success, conflict stop, worker budget exhaustion
  stops only that worker, replay equality); all quality gates green.

## Out of scope

- model-driven or dynamic task decomposition (the decomposition plan is
  code-owned this cycle; learned/adaptive decomposition is PACS-017);
- re-dispatching failed workers or iterative merge repair (a failed worker or
  a merge conflict stops the orchestrated run explicitly — recovery policy is
  later work);
- wall-clock parallel worker execution (threads/async);
- evaluator/reflection loops, approval gateways, async HITL (PACS-014);
- provenance graph / trajectory debugger (PACS-015);
- multi-trial benchmarking and statistical comparison of single- vs
  multi-agent runs (PACS-016) — this cycle only makes the path benchmarkable;
- new live provider adapters; live multi-worker Ollama evidence beyond the
  existing probe-gated patterns;
- changes to `ControlPolicy`, `PermissionPolicy`, budgets-as-authority,
  sandbox capability contracts, or the single-runtime repair demo byte stream.

## Plan

1. **Domain vocabulary** (`domain/orchestration.py`): `WorkerOutcome` and
   `MergeOutcome` closed vocabularies, `WorkerSpec` (validated spawn-time
   assignment: worker id, run id, workspace id, objective, budget share),
   `WorkerProjection` (roster element), `partition_budget` (fail-closed share
   validation: count ≥ 1, shares sum ≤ global, finite/positive).
2. **Events + reducer + codec + telemetry** (the four touchpoints, together):
   three event dataclasses with construction validation (bounded text,
   enum coercion); `RunState.workers` roster + three reducer arms
   (`WorkerSpawned` READY/ACTING→ACTING; `WorkerStopped` roster outcome;
   `WorkerMerged` roster merge outcome + VERIFYING gate); strict codec
   constructors (`_required_*` family); metadata-only projector arms with
   `loopforge.worker_id`; closed `MetricName` additions.
3. **Runtime step seam**: extract the `_drive` cycle body; per-run drive
   state; public `step(run_id)`; optional `worker_id` threaded into
   `RuntimeTelemetry` correlation. `run()`/`resume()` semantics pinned
   unchanged by the existing suite.
4. **Worktree adapter**: `GitWorkspaceManager.materialize_integration` +
   linked-worktree add/remove/commit/merge on a code-owned integration repo;
   fingerprint extended to the worktree layout; conflict → typed result.
5. **Orchestrator**: spawn (bounded, `WorkerSpawned`), interleaved drive
   (round-robin `step`), terminal collection (`WorkerStopped`), spawn-order
   merge (`WorkerMerged`), integration verification + artifacts, durable
   stops; defense-in-depth aggregate budget check; `close()` fan-out over
   worker bundles mirroring the PACS-012 contract.
6. **Workload + entrypoints**: two-module fixture + code-owned decomposition;
   `orchestrated-repair-demo` CLI; scripted default; per-worker registry
   routing and context builders; integration verifier + evidence on the
   merged workspace.
7. **Tests** per the in-scope list; event-catalog pins updated 19→22.
8. **Gates**: `uv run pytest -q --cov` (branch ≥ 90%),
   `uv run ruff format --check . && uv run ruff check .`, `uv run pyright`
   (strict), `uv run lint-imports`.

## Act

- **Domain vocabulary** (`domain/orchestration.py`): closed `WorkerOutcome` /
  `MergeOutcome` vocabularies; `validate_worker_id` / `validate_worker_text` /
  `validate_budget_share` fail-closed validators; `WorkerSpec` spawn-time
  assignment; `WorkerProjection` roster element; `partition_budget` static
  shares (cost/tokens additive, validated to never sum above the global limit
  — float rounding nudged downward; iterations/elapsed inherited per worker).
- **Event catalog 19→22, schema v1** (operator-signed-off): `WorkerSpawned`,
  `WorkerStopped`, `WorkerMerged` through all four touchpoints — `Event`
  union, reducer arms + `_ALLOWED_STATUS`, JSON codec registry/constructors
  (schema stays 1), metadata-only telemetry projector arms threading
  `worker_id` into the reserved `CorrelationIds.worker_id` slot, plus closed
  `MetricName` worker-lifecycle additions. `RunState.workers` projects the
  roster; the `WorkerMerged` arm transitions the orchestrated run to
  `VERIFYING` exactly when every spawned worker is stopped and every
  succeeded worker has a merge outcome; duplicate spawns, double terminal
  outcomes, merges by non-succeeded workers, and unknown workers are rejected
  by the reducer.
- **Runtime step seam** (`application/runtime.py`): drive-loop locals became
  per-run `_DriveState` (failure streak, active model, fallback flag) so the
  extracted `_drive_cycle` advances exactly one cycle via public
  `step(run_id)`; blocking `run()`/`resume()` semantics unchanged (pinned by
  the pre-existing suite, including the transient-failure fallback tests).
  `Runtime` gained optional `worker_id` for telemetry correlation.
- **Worktree isolation** (`adapters/git_workspace.py`): one code-owned
  integration repository per orchestrated run; `add_worker_worktree`
  (`git worktree add -b worker/<id>` from the integration base revision),
  `commit_worker`, `merge_worker` (spawn-order `--no-ff --no-edit`; conflict
  → `merge --abort` → `None`, never a silent resolution); the
  metadata-fingerprint defense extended to the linked-worktree `.git` pointer
  file so a tampered pointer fails closed before any host-Git invocation.
- **Durable orchestrator** (`application/orchestrator.py`, workload-agnostic):
  owns the global plan on its own authoritative stream; spawns bounded
  workers (`max_workers`, unique ids, shares ≤ global validated at
  construction) with durable `WorkerSpawned` ownership records (worker id,
  worker run id, workspace id, objective, budget share); drives worker
  runtimes round-robin one cycle at a time (deterministic interleaving —
  logical concurrency, zero wall-clock races, trivial replay); records
  `WorkerStopped` outcomes; cancels remaining workers explicitly if the
  defense-in-depth aggregate cost check trips; reconciles succeeded workers
  in spawn order with durable `WorkerMerged` (MERGED with revision, CONFLICT
  without); verifies the merged workspace through a verifier bound to the
  integration workspace; records merged evidence via the existing
  `ArtifactRecorded` path (fail-closed on collector failure); stops through
  the existing `RunStopped` with reason codes `WORKER_INCOMPLETE`,
  `WORKER_MERGE_CONFLICT`, `WORKER_MERGE_ERROR`,
  `MERGED_VERIFICATION_FAILED`, `ORCHESTRATOR_BUDGET_EXHAUSTED`,
  `ARTIFACT_COLLECTION_FAILED` — and `ORCHESTRATOR_CONTROL_INCONSISTENT` if a
  passed verification ever produced no control stop.
- **Workload + entrypoints**: `WorkerRepairAssignment` /
  `OrchestratedRepairTask` (code-owned decomposition; model output never
  decides who repairs what); the two-module `calculator` fixture (independent
  buggy `adder.py`/`greeter.py`, disjoint per-worker patch constraints,
  per-worker scoped test commands); `entrypoints/orchestrated.py` composition
  root (per-worker worktree/sandbox/verifier/context/routing/budget share,
  `close()` fan-out mirroring the PACS-012 contract); the
  `orchestrated-repair-demo` CLI command. The single-runtime `repair-demo`
  path is untouched — multi-agent is opt-in, never a default.
- **Tests**: 38 domain-vocabulary unit tests; 12 reducer-arm roster tests; 15
  durable-orchestrator unit tests (interleave order, durable spawn ownership,
  merge order, conflict/error stops, merged-verification failure, aggregate
  budget defense canceling siblings, artifact evidence + fail-closed
  collection, worker-attributed telemetry, construction guards); worktree
  adapter isolation/merge/conflict tests; event-catalog pins 19→22 across
  `test_events`/`test_json_events`/`test_telemetry_projection`; integration
  E2E (`test_orchestrated_repair_runtime.py`): two-worker trusted-local run
  (RLIMIT_AS-gated), two-worker live container run, and a constructed
  same-file conflict run proving explicit rejection (RLIMIT_AS-gated); CLI
  pins (trusted-local success gated, empty/flag-like `--container`
  rejection); help-text updated for the new command.
- **Discoveries fixed and pinned**: the integration acceptance's
  `require_change` was incompatible with committed merges — worker patches
  land as merge *commits*, so status-based change detection sees a clean
  tree (`MERGED_VERIFICATION_FAILED: no workspace changes` on a fully
  repaired fixture). Integration acceptance now gates on the full merged
  suite with `require_change=False` (per-worker acceptance enforces
  `require_change` pre-merge; prefix/file-count constraints still reject
  stray uncommitted edits). Container runs must wire the in-container
  interpreter (`/usr/local/bin/python`) — the host `sys.executable` default
  is only valid for the trusted-local path.
- **Mid-cycle scope additions** (operator-visible commits `d12d71a`..
  `3a26f27`, outside the planned scope's "no new live provider adapters"
  boundary, recorded here as a deviation): a DeepSeek live adapter, existing
  repository adoption (`adopt_existing`), and the `civicml-loop` dogfooding
  command. They do not affect the orchestrator architecture and are pinned by
  their own suites.

## Check

Verified in this environment (2026-08-30, Ollama 0.32.15 with
`devstral-small-2:latest`, Docker Desktop live, `python:3.12-alpine` pulled):

- `uv run pytest -q --cov` — **1449 passing, 18 skipped** (skips are the
  pre-existing macOS `RLIMIT_AS` platform gates + 1 non-UTF-8-filesystem gate
  + 3 new trusted-local orchestrated E2E/CLI gates on the same platform
  restriction; the live Ollama+Docker repair E2E and the live container
  orchestrated E2E both **executed and passed**; zero credential-gated skips)
- deterministic CI — `--ignore=tests/live`: **1448 passing, 18 skipped** with
  zero provider credentials
- branch-aware coverage — **94.34% overall**; configured 90% gate satisfied;
  new modules: `domain/orchestration.py` 98%, `application/orchestrator.py`
  92%, `entrypoints/orchestrated.py` 93%
- `ruff format --check .` / `ruff check .` — clean (148 files)
- `pyright` (strict) — 0 errors, 0 warnings
- `lint-imports` — 2 contracts kept, 0 broken
- live CLI evidence — `orchestrated-repair-demo --container
  python:3.12-alpine` → `status=succeeded stop_reason=success_verified`,
  `worker=adder outcome=succeeded merge=merged`, `worker=greeter
  outcome=succeeded merge=merged`, integration verifier
  `command:run_tests: passed (exit_code=0)`, merged evidence artifact
  recorded; orchestrator stream `RunStarted, PlanCreated, WorkerSpawned ×2,
  WorkerStopped ×2, WorkerMerged ×2, VerificationPassed, ArtifactRecorded,
  RunStopped` replays to the exact terminal state
- acceptance gate (remaining-pacs-plan.md): (1) two workers execute without
  sharing a mutable filesystem workspace — linked worktrees, pinned by
  adapter isolation tests and both E2E runs; (2) conflicting state updates
  rejected explicitly — `WorkerMerged(CONFLICT)` + `WORKER_MERGE_CONFLICT`
  `FAILURE` stop, pinned at unit level (executed) and E2E level
  (platform-gated); (3) global budgets enforceable across workers —
  `partition_budget` shares ≤ global (construction- and unit-pinned),
  per-worker `ControlPolicy` enforcement, orchestrator aggregate defense
  check (unit-pinned, cancels siblings explicitly); (4) orchestrator owns the
  global plan, workers own assigned workspaces — durable `WorkerSpawned`
  ownership records pin the binding; (5) multi-agent is a benchmarkable path,
  not the default — `repair-demo` byte behavior pinned unchanged by the
  pre-existing replay/CLI suites; `orchestrated-repair-demo` is a separate
  opt-in command.

## Stop

Classification: **SUCCESS**. All five acceptance criteria are met with
executed evidence (two platform-gated trusted-local E2E pins execute on
RLIMIT_AS-capable platforms, matching the posture of every prior cycle).
The post-cycle adversarial hardening pass is available on operator request,
following the PACS-010/011/012 pattern.

## Follow-on implications

- PACS-016 (locked benchmark) now has a real multi-agent path to compare
  against the single-runtime baseline: `calculator_repair_task` +
  `orchestrated-repair-demo` are deterministic and replayable.
- Recovery policy is deliberately absent: a failed worker or a merge
  conflict stops the orchestrated run explicitly. Re-dispatching failed
  workers or iterative merge repair is future work (needs an operator
  decision on recovery authority).
- The runtime `step()` seam is the canonical way to drive a runtime from
  outside; PACS-014's async HITL should consume the same seam rather than
  adding a second drive path.
- Trusted-local orchestrated E2E (success + conflict) is RLIMIT_AS-gated and
  skips on macOS; Linux CI executes it. The container E2E covers the
  untrusted boundary on any Docker host.
- The post-cycle adversarial hardening pass (three-agent review) has not
  run; on operator initiation, findings get fixed, pinned in
  `tests/regression/test_hardening_regressions.py` (PACS-013 section), and
  recorded here.
