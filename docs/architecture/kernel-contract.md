# Runtime kernel contract

The v0.1 kernel establishes the deterministic authority boundary that later persistence, reliability, sandbox, context, model, and orchestration layers must preserve.

## Model-owned data

A model may propose only the intent of an action:

- action ID
- registered tool name
- tool arguments
- optional expected observation

A model does **not** declare its own risk, permission requirement, retry behavior, idempotency semantics, approval requirement, timeout, or data sensitivity.

## Code-owned tool metadata

Every registered tool has immutable `ToolMetadata` owned by the runtime/tool adapter:

- `RiskLevel`
- `Permission`
- `SideEffectClass`
- `RetryClass`
- `IdempotencyClass`
- `ApprovalClass`
- timeout
- `DataSensitivity`

The metadata constructor rejects internally inconsistent contracts, including:

- a risk level paired with a weaker permission
- a retryable side-effecting tool without natural/keyed idempotency
- an irreversible tool without required human approval
- non-positive timeouts

`ActionAuthorized` snapshots the exact tool metadata used for the decision so later replay/audit can establish what policy governed the side effect at execution time.

## Event contract

Domain events are immutable, keyword-only dataclasses. Every event requires:

- event ID
- run ID
- timezone-aware occurrence timestamp
- positive stream sequence
- optional causal event ID

`replay()` rejects events from a different run and non-contiguous stream sequences.

## Serialization boundary

`EventCodecPort` defines the stable serialization boundary for future durable event stores. The v0.1 reference adapter is `JsonEventCodec`.

The JSON envelope contains:

```json
{
  "schema_version": 1,
  "event_type": "RunStarted",
  "event": {}
}
```

The current codec:

- round-trips every event in the current catalog
- serializes timestamps explicitly
- rejects unsupported schema versions
- rejects unknown event types
- validates nested action, usage, and tool-metadata structures while decoding

The schema version is intentionally explicit before SQLite persistence is introduced so storage does not become coupled to implicit Python object layout.

## Authority invariant

> The model may select a capability; it may not redefine the capability's authority or reliability semantics.
