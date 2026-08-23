# PACS-003 — Reliability control plane

Level: capability
Status: COMPLETE
Parent: product build map
Target milestone: v0.2 durable reliability foundation

## Plan

### Current state

PACS-002 provided append-only durable event streams, optimistic concurrency, deterministic replay, and safe resume from quiescent states. Recovery from ambiguous `ACTING` state deliberately failed closed because no durable action-attempt/idempotency semantics existed.

### Objective

Add runtime-owned retry/backoff/idempotency/circuit-breaker/no-progress semantics and close the ambiguous-action recovery gap without weakening deterministic authority or claiming generic exactly-once execution.

### Invariants / constraints

- model output cannot decide retry safety;
- tool metadata remains code-owned;
- external execution is at-least-once unless the adapter provides stronger semantics;
- ambiguous non-idempotent writes fail closed;
- retry/idempotency history must be durable and replayable;
- retry delay must survive process restart;
- hard resource policy cannot be overridden by model output;
- PACS-004 systematic fault laboratory is not silently initiated.

### Acceptance evidence

- timeout/ambiguous remote-success scenario does not duplicate a side effect;
- a process restart during an ambiguous keyed action replays the same attempt/key safely;
- transient failures retry according to bounded backoff policy;
- permanent failures do not retry;
- retry schedule/backoff survives restart;
- repeated dependency failures open a circuit and block later calls;
- repeated non-improving verification terminates `STALLED` before max iterations;
- USD/token/time/iteration controls remain deterministic;
- operator cancellation produces an explicit terminal state;
- old PACS-001/002 `ToolFailed(retryable=...)` schema-v1 payloads remain decodable;
- full deterministic suite, branch coverage, compile, demo, architecture, and diff checks pass.

### Expected files/systems touched

- reliability domain policy/types
- tool execution request/result contract
- action lifecycle domain events/state projection
- runtime resume/execute/retry flow
- event codec compatibility
- clock/sleeper ports and adapters
- integration/unit tests
- architecture docs, ADR, roadmap/build status

### Risks

- treating retry as equivalent to safe replay;
- duplicate side effects after timeout/response loss;
- losing backoff on restart;
- action journal state drifting from authoritative run history;
- circuit-breaker or no-progress heuristics stopping legitimate work too aggressively;
- breaking replay of pre-PACS-003 schema-v1 streams.

## Act

- Added `ToolFailureClass`, `RetrySettings`, `RetryDecision`, and `ReliabilityPolicy`.
- Replaced the old tool-result `retryable` Boolean with failure classification; retry permission now derives from tool contract + failure class + attempt budget.
- Added `ToolExecutionRequest` containing attempt, per-tool timeout contract, and idempotency key.
- Added `ToolExecutionStarted`, `RetryScheduled`, and `CircuitOpened` events; enriched tool outcome events with attempt number/failure class.
- Used the authoritative event stream as the durable action journal rather than introducing a dual-write journal table.
- Added deterministic run-scoped keyed idempotency (`loopforge:<run_id>:<action_id>`).
- Added safe `ACTING` recovery: idempotent/read-only attempts replay; ambiguous non-idempotent side effects fail closed.
- Added durable retry `retry_not_before` projection so restart honors remaining backoff.
- Added bounded exponential backoff with deterministic jitter and injectable sleeper.
- Added tool-scoped per-run circuit breakers.
- Added deterministic no-progress tracking from verifier score/summary history.
- Extended budgets with total-token and elapsed-time constraints and added explicit operator cancellation.
- Preserved JSON schema-v1 replay compatibility for legacy `ToolFailed(retryable=...)` payloads.

## Check

### Checks executed

- `PYTHONPATH=src python -m pytest -q` — 96 passing
- `PYTHONPATH=src python -m coverage run --branch -m pytest -q` — 96 passing
- `PYTHONPATH=src python -m coverage report -m` — 92% overall branch-aware coverage; 90% gate satisfied
- `PYTHONPATH=src python -m loopforge.entrypoints.cli demo` — verified success after two action/observation cycles
- `python -m compileall -q src tests` — passing
- architecture DAG tests — included in pytest suite
- `git diff --check` — passing
- 100-character Python source/test line-length scan — passing

### Acceptance review

- ambiguous remote success retries with one actual side effect: PASS
- restart during keyed ambiguous action reuses the same run-scoped key/attempt: PASS
- transient failure retry/backoff is bounded and deterministic: PASS
- permanent failure is not retried: PASS
- persisted retry schedule preserves remaining backoff after restart: PASS
- repeated tool failures open a circuit and later calls are blocked: PASS
- repeated non-improving verifier results terminate `STALLED`: PASS
- USD/token/time/iteration controls remain code-owned: PASS
- operator cancellation produces explicit terminal state: PASS
- legacy schema-v1 `retryable` tool-failure payload remains replay-decodable: PASS
- action journal rejects mismatched action IDs and non-sequential attempts: PASS

### Diff/repository review

PACS-003 changes are bounded to reliability/governance semantics, their durability/replay requirements, and supporting docs/tests. PACS-004 remains a separate planned systematic fault-injection phase.

## Stop

Classification: SUCCESS

### Result

LoopForge now has a durable, runtime-owned reliability control plane. External side effects can be retried/recovered only under explicit tool contracts; keyed ambiguous outcomes reuse stable idempotency keys; backoff and retry state survive restart; circuit/no-progress/resource policies constrain repeated autonomy; and every terminal reliability decision remains explicit in durable state.

### Remaining issues

- per-tool timeout is an execution-adapter contract; generic hard process cancellation awaits sandbox execution;
- circuit state is per-run, not provider/fleet health;
- current progress detection is deterministic and conservative rather than semantic;
- systematic failure matrix/chaos harness remains PACS-004;
- Ruff/Pyright/Import Linter still require an environment with declared dev tooling installed.

### Candidate next cycles

- PACS-004 — Fault-injection laboratory (`PLANNED` only)
- PACS-005 — Security + sandbox contract (`PLANNED` only)
