# PACS-002 — Durable SQLite event store

Level: capability
Status: COMPLETE
Parent: product build map
Target milestone: v0.2 durable reliability foundation

## Plan

### Current state

PACS-001 provided immutable domain events, deterministic replay, a versioned JSON event codec, and an in-memory store. Runtime execution was not durable and the state-store port had no explicit concurrency contract.

### Objective

Add a durable SQLite event stream with expected-version optimistic concurrency and safe replay/resume semantics, while preserving the existing event/domain boundaries.

### Invariants / constraints

- event history remains append-only;
- storage serialization goes through `EventCodecPort`;
- stale writers fail instead of silently reordering events;
- database schema versioning is distinct from event-envelope schema versioning;
- no retry/backoff/circuit-breaker/idempotency-journal implementation in this cycle;
- recovery from ambiguous `ACTING` state must fail closed until PACS-003.

### Acceptance evidence

- SQLite store initializes and reopens a versioned schema idempotently;
- durable event payloads round-trip and replay to identical state;
- concurrent/stale writers produce an explicit version conflict;
- event rows cannot be updated or deleted;
- a persisted `READY` run resumes in a fresh runtime instance;
- a persisted `VERIFYING` run repeats verification without repeating its prior tool action;
- ambiguous `ACTING` recovery is rejected;
- full deterministic test suite, branch-aware coverage gate, compile check, architecture tests, and demo pass.

### Expected files/systems touched

- `ports/state_store.py`
- in-memory state-store adapter
- new SQLite event-store adapter
- runtime start/resume persistence flow
- integration/unit tests
- persistence architecture docs + ADR
- roadmap/build status/product map

### Risks

- accidental coupling of SQLite to domain event construction;
- migration and event schema versions becoming conflated;
- false claims of safe process recovery before idempotency exists;
- optimistic concurrency implemented as a non-atomic check-then-write.

## Act

- Changed `StateStorePort` to compare-and-append with `expected_version`, `current_version`, explicit `StreamVersionConflictError`, and duplicate-event handling.
- Updated the in-memory store to implement the same semantics as durable stores.
- Added `SQLiteEventStore` using short SQLite transactions and `BEGIN IMMEDIATE` to atomically compare stream version and append.
- Added an independently versioned store migration table and version-1 event schema.
- Added database triggers rejecting UPDATE and DELETE against persisted events.
- Refactored runtime into `start()`, `state_for()`, and `resume()` so a quiescent run can survive runtime/process replacement.
- Added safe recovery semantics for `PLANNING`, `READY`, `REFLECTING`, and `VERIFYING` states.
- Deliberately fail closed from `ACTING` until the PACS-003 action/idempotency journal exists.
- Added concurrency, migration, durable replay, restart, append-only, and unsafe-resume tests.
- Added ADR-0006 and durable event-store architecture documentation.

## Check

### Checks executed

- `PYTHONPATH=src python -m pytest -q` — 74 passing
- `PYTHONPATH=src python -m coverage run --branch -m pytest -q` — 74 passing
- `PYTHONPATH=src python -m coverage report -m` — 93% total branch-aware coverage; 90% gate satisfied
- `PYTHONPATH=src python -m loopforge.entrypoints.cli demo`
- `python -m compileall -q src tests`
- architecture DAG tests (part of pytest suite)
- `git diff --check`
- source line-length scan

### Acceptance review

- SQLite schema initializes/reopens at version 1: PASS
- serialized events survive DB reopen and deterministic replay: PASS
- stale writer conflict: PASS
- actual two-thread writer race yields exactly one commit and one conflict: PASS
- event UPDATE/DELETE blocked by DB triggers: PASS
- fresh runtime resumes persisted READY stream: PASS
- VERIFYING resume does not repeat tool side effect: PASS
- ACTING resume fails closed: PASS
- SQLite/event serialization remain adapter/port concerns outside domain: PASS

### Diff/repository review

The diff is bounded to durable state-store semantics, runtime resumption, tests, and supporting architecture/process documentation. Retry scheduling, idempotency journal, circuit breakers, and no-progress detection remain untouched for PACS-003.

## Stop

Classification: SUCCESS

### Result

LoopForge now has an append-only durable SQLite event history with atomic expected-version concurrency, deterministic state reconstruction, and explicitly bounded resume semantics. A run can be persisted, reconstructed by a fresh runtime, and continued without relying on conversation/process memory.

### Remaining issues

- `ACTING` recovery is intentionally unsafe until an action/idempotency journal exists.
- concurrency conflicts are detected but not automatically retried/reconciled.
- full-stream replay has no snapshot optimization yet.
- external Ruff/Pyright/Import Linter execution remains dependent on a development environment where those configured tools are installed.

### Candidate next cycles

- PACS-003 — Reliability control plane (`PLANNED` only)
- PACS-004 — Fault-injection laboratory (`PLANNED` only)
