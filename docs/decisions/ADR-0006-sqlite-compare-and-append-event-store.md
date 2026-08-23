# ADR-0006 — SQLite compare-and-append event store

Status: Accepted

## Context

LoopForge requires durable run history without allowing persistence to weaken the deterministic event-stream contract established in v0.1. Multiple runtime instances may observe the same stream version and attempt to append concurrently. Silent last-write-wins behavior would make replay nondeterministic and could hide lost updates.

Durable storage must also preserve the versioned event codec boundary rather than serializing Python objects implicitly.

## Decision

The local reference persistence adapter is SQLite with an append-only `events` table.

Every append uses expected-version optimistic concurrency:

1. the caller supplies `expected_version`;
2. the store begins an immediate transaction;
3. the store reads the authoritative current sequence for the run;
4. if actual != expected, append fails with `StreamVersionConflictError`;
5. the event sequence must equal `expected_version + 1`;
6. the encoded event is inserted and committed atomically.

The database schema is migration-versioned independently from the event-envelope schema. Domain event payloads are encoded through `EventCodecPort` / `JsonEventCodec`.

The events table rejects UPDATE and DELETE through database triggers so append-only behavior is enforced beneath the adapter API.

## Recovery boundary

PACS-002 permits durable resume only where repeating external side effects is not ambiguous:

- `READY`: continue normally;
- `VERIFYING`: re-run verification only;
- `REFLECTING` / `PLANNING`: re-plan and continue;
- terminal / waiting-for-approval states: return without autonomous execution;
- `ACTING`: fail closed.

`ACTING` cannot be recovered safely yet because an external tool may have completed while its acknowledgment was lost. PACS-003 will add the action/idempotency journal needed to resolve that ambiguity.

## Consequences

Positive:

- replay remains deterministic across process/runtime instances;
- stale concurrent writers fail explicitly;
- SQLite is sufficient for a reproducible local reference implementation;
- provider/persistence details remain outside the domain layer;
- later stores can implement the same compare-and-append port contract.

Costs:

- runtime writes may receive concurrency conflicts and must eventually define retry/reconciliation policy;
- snapshots are deferred; state reconstruction currently replays the full stream;
- SQLite is not intended to be the final answer for high-throughput distributed deployment.
