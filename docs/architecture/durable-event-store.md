# Durable event store

PACS-002 adds the first durable implementation of LoopForge's authoritative event history.

## Two independent versions

LoopForge intentionally separates:

1. **event envelope schema version** — how a domain event is serialized (`JsonEventCodec`);
2. **store schema version** — how SQLite tables/indexes/triggers are laid out.

This prevents database migrations from becoming implicit event migrations and lets historical event payloads remain replayable independently of storage implementation changes.

## Append contract

The store is compare-and-append, not append-at-latest:

```text
writer reads stream version 8
          │
          ▼
creates event sequence 9
          │
          ▼
append(expected_version=8)
          │
    ┌─────┴─────┐
    │           │
actual=8     actual=9
    │           │
 COMMIT      CONFLICT
```

A stale writer receives `StreamVersionConflictError`. The store never silently renumbers the stale event because doing so would change the causal history the caller believed it was extending.

## SQLite reference schema

The reference adapter stores:

- `run_id`
- stream `sequence`
- globally unique `event_id`
- `event_type`
- `occurred_at`
- versioned JSON `payload`

Primary key: `(run_id, sequence)`.

`event_id` is globally unique. UPDATE and DELETE are rejected by SQLite triggers, making the table structurally append-only.

## Resume semantics

`Runtime.start()` persists a run through its first `READY` checkpoint without executing a tool. `Runtime.resume(run_id)` reconstructs `RunState` from durable events and continues only from states whose recovery semantics are currently known to be safe.

Safe in PACS-002:

- `READY`
- `PLANNING`
- `REFLECTING`
- `VERIFYING` (verification is repeated, tool execution is not)

No automatic execution:

- terminal states
- `WAITING_FOR_APPROVAL`

Fail closed:

- `ACTING`

The `ACTING` boundary is intentional. Until an action journal and idempotency keys exist, LoopForge cannot know whether a side effect occurred immediately before a crash. Pretending that state is safely retryable would violate the runtime's reliability thesis.

## Snapshot policy

There are no state snapshots yet. Replay correctness is the source of truth; snapshot/projection optimization is deferred until profiling demonstrates a need. Any future snapshot must be discardable and reproducible from the event stream.
