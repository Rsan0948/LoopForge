# ADR-0007 — Event-backed action journal and runtime-owned retry policy

Status: Accepted

## Context

PACS-002 made run history durable but intentionally failed closed when a process restarted in `ACTING`, because the runtime could not know whether an external side effect had already occurred.

Adding reliability creates two design risks:

1. maintaining a separate action journal beside the authoritative event stream would create a dual-write consistency boundary;
2. allowing a model or tool-result Boolean to decide retries would let probabilistic/application-local behavior control side-effect safety.

## Decision

The authoritative run event stream is also the action journal.

Before tool execution, LoopForge appends `ToolExecutionStarted` with attempt number and, when required, the idempotency key. Tool outcomes and retry schedules are appended to the same stream.

Retry authorization is derived by `ReliabilityPolicy` from code-owned tool metadata, observed `ToolFailureClass`, attempt count, and retry settings.

For keyed-idempotent operations, the idempotency key is deterministic and run-scoped: `loopforge:<run_id>:<action_id>`.

LoopForge assumes at-least-once execution for external side effects unless an adapter explicitly provides stronger semantics. Ambiguous non-idempotent writes fail closed.

## Consequences

Positive:

- one authoritative history for state and execution recovery;
- no event-store/action-journal dual-write transaction;
- repeatable analysis of every attempt and retry decision;
- safe recovery from ambiguous keyed/naturally idempotent execution;
- retry authority remains outside the model.

Trade-offs:

- the event stream carries more operational detail;
- keyed idempotency requires cooperating external adapters/services;
- generic exactly-once semantics are explicitly not provided;
- run-scoped circuit state is local and does not model fleet/provider health.
