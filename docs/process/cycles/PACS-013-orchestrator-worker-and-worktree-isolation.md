# PACS-013 — Orchestrator/worker and worktree isolation

Status: MINIMUM FUNCTIONAL CHECKPOINT

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

- Added replayable worker lifecycle vocabulary and projections, per-run runtime stepping,
  worker-attributed telemetry, deterministic round-robin orchestration, and budget partitioning.
- Added isolated linked Git worktrees, worker commits, spawn-order merges, and conflict aborts.
- Added a DeepSeek live adapter and CLI backend so the functional path can be dogfooded now.
- Deferred the full orchestrated-repair CLI fixture and broader adversarial hardening described
  above; this checkpoint intentionally targets usable functionality rather than PACS closure.

## Check

- Credential-free suite: 1,367 passing, 29 platform-gated skips; 93.82% branch coverage.
- Ruff format/check, Pyright strict, and import-linter green.
- Live DeepSeek Pro + Docker repair: succeeded in 3 iterations, approximately $0.01; independent
  verifier passed tests and patch constraints, with exact `adder.py` evidence recorded.

## Stop

Minimum functional checkpoint reached. PACS-013 remains intentionally short of its complete
hardening scope, but the bounded worker/worktree primitives and live DeepSeek repair path are
usable for initial dogfooding.

## Follow-on implications

(recorded at cycle end)
