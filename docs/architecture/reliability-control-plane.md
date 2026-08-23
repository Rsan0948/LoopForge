# Reliability control plane

PACS-003 turns LoopForge's persisted action lifecycle into a reliability control plane. The model still proposes actions; retry, replay, budgets, circuit state, and stall termination are runtime-owned policy.

## Event-backed action journal

LoopForge does not introduce a second authoritative action-journal database. The append-only run stream is the journal:

```text
ActionProposed
  → ActionAuthorized
  → ToolExecutionStarted(attempt, idempotency_key)
  → ToolSucceeded | ToolFailed(failure_class, attempt)
  → RetryScheduled(next_attempt, delay)
  → ToolExecutionStarted(...)
```

This avoids a dual-write consistency problem between a run event stream and a separate journal. `ToolExecutionStarted` is durably appended before the side effect begins. If the process disappears after remote execution but before a result event, replay reconstructs an `ACTING` state with `execution_in_flight=true`.

## At-least-once execution and ambiguous outcomes

LoopForge does not claim generic exactly-once distributed execution.

If an interrupted action may have executed remotely:

- pure/read-only actions may be replayed;
- naturally idempotent actions may be replayed;
- keyed-idempotent actions may be replayed with the same persisted key;
- non-idempotent side effects fail closed.

Keyed idempotency keys are scoped to both run and logical action:

```text
loopforge:<run_id>:<action_id>
```

A retry or process restart reuses that same key.

## Failure classification vs retry authorization

`ToolResult` reports what happened using an operational failure class:

- `TRANSIENT`
- `PERMANENT`
- `AMBIGUOUS_OUTCOME`

It does **not** decide whether the runtime retries.

Retry authorization combines:

1. the code-owned tool contract (`RetryClass`, `IdempotencyClass`, side-effect class);
2. the observed failure class;
3. the current attempt number;
4. the configured retry budget.

This keeps retry policy outside both the model and the tool's error prose.

## Backoff and jitter

Retry delay uses bounded exponential backoff with deterministic jitter derived from the action/attempt identity. The exact delay is persisted in `RetryScheduled`.

`RunState.retry_not_before` is projected from the event timestamp plus the delay. A process restart therefore honors the remaining backoff instead of immediately retrying.

The sleeper is a port so deterministic tests can record delays without wall-clock sleeping.

## Circuit breakers

LoopForge tracks consecutive failures per tool name. Once the configured threshold is reached, `CircuitOpened` is persisted and later proposals for that tool are rejected with `BLOCK_CIRCUIT_OPEN`.

The circuit is scoped to the run in PACS-003. Cross-run/provider health aggregation is deliberately deferred.

## No-progress detection

The state projection tracks repeated failed verification without measurable improvement.

- numeric verifier scores reset the counter only when a new best score is reached;
- scoreless verifiers treat a changed summary as potential progress and identical repeated summaries as non-progress.

`ControlPolicy.no_progress_limit` turns repeated non-improvement into `STOP_STALLED_NO_PROGRESS` / `RunStatus.STALLED` before the maximum-iteration boundary.

This is intentionally conservative. Semantic trajectory-level stall detection remains a later experimental capability.

## Resource limits

The control plane supports:

- USD budget;
- model token budget;
- maximum authorized action iterations;
- elapsed run budget checked at deterministic control boundaries;
- explicit operator cancellation.

The runtime rechecks model-incurred budgets before authorizing a tool side effect.

Per-tool `timeout_seconds` is included in `ToolExecutionRequest`; the execution adapter/sandbox is responsible for honoring that deadline. PACS-003 deliberately does not fake generic hard cancellation of an arbitrary in-process side effect.

## Recovery matrix

| Persisted state | Recovery behavior |
| --- | --- |
| `READY` | choose next action |
| `PLANNING` / `REFLECTING` | reconstruct plan checkpoint |
| `VERIFYING` after success | rerun verifier only |
| `VERIFYING` after failure | derive retry vs verify from policy |
| `ACTING`, not yet started | execute first/next attempt |
| `ACTING`, started + idempotent | replay same attempt/key |
| `ACTING`, started + non-idempotent write | fail closed |
| terminal | return reconstructed terminal state |

## What PACS-003 does not do

- provider-wide/cross-run circuit health;
- sandbox-enforced process termination;
- network-level retry adapters;
- systematic chaos/fault matrix;
- semantic learned progress detection;
- human approval routing.

Those belong to later manually initiated cycles.
