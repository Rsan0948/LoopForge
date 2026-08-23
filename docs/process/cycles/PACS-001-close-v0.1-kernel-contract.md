# PACS-001 — Close the v0.1 kernel contract

Level: capability
Status: COMPLETE
Target milestone: Milestone 0 — Runtime kernel

## Plan

### Current state

The repository already had immutable events, deterministic state projection, explicit legal transitions, provider-independent ports, an in-memory event store, budget/permission policy, scripted adapters, architecture tests, and a two-cycle verifier-driven demo. The product map identified the remaining kernel-contract work as strict tool metadata, a versioned event-serialization skeleton, stronger invariant/property-style testing, and documentation reconciliation.

### Objective

Close the v0.1 in-memory kernel contract without beginning durable SQLite persistence or the PACS-003 reliability mechanisms.

### Invariants / constraints

- model output must not own runtime authority
- domain remains provider/infrastructure agnostic
- state changes only through events
- no live LLM dependency
- no SQLite persistence in this cycle
- no retry engine/circuit breaker/no-progress implementation in this cycle
- future persistence must consume a serialization contract rather than invent one inside the store

### Acceptance evidence

- every registered tool can express risk, permission, side effect, retry, idempotency, approval, timeout, and sensitivity semantics
- model proposals cannot self-declare these semantics
- internally inconsistent tool contracts fail construction
- all current domain events round-trip through a versioned codec
- unsupported event schema versions/types fail closed
- stronger event/replay invariants have deterministic tests
- architecture tests remain green
- configured 90% coverage gate is met
- runtime demo still completes only after external verification
- documentation accurately separates v0.1 kernel completion from PACS-002 persistence

### Expected files/systems touched

Domain action/event/tool contracts, tool/event-codec ports, scripted/JSON adapters, runtime authorization path, deterministic tests, architecture/roadmap/build-status documentation.

### Risks

The existing action proposal included a model-declared risk level; leaving it intact would undermine the intended authority boundary even if metadata types were added alongside it.

## Act

- Added immutable `ToolMetadata` plus `SideEffectClass`, `RetryClass`, `IdempotencyClass`, `ApprovalClass`, and `DataSensitivity`.
- Moved risk/permission semantics out of `ActionProposal`; the model now proposes only tool intent and arguments.
- Added tool metadata lookup to `ToolExecutorPort` and made unknown tools fail closed before execution.
- Changed `PermissionPolicy` to authorize the code-owned tool contract rather than model output.
- Added `tool_metadata` snapshot to `ActionAuthorized` for future audit/replay.
- Added event timestamp/sequence invariants and cross-run replay protection.
- Added `EventCodecPort` and version-1 `JsonEventCodec` covering the complete current event catalog.
- Added deterministic round-trip, malformed-envelope, tool-contract, authorization, unknown-tool, event, replay, CLI, and coverage tests.
- Reconciled roadmap/build-status/architecture docs so SQLite is explicitly PACS-002.

Material deviation from the initial expectation: the tool-metadata work required removing the existing model-owned `risk` field rather than merely adding metadata beside it. This was necessary to satisfy the project's authority invariant.

## Check

### Checks executed

- `PYTHONPATH=src python -m pytest -q` → **60 passed**
- `PYTHONPATH=src python -m coverage run --branch -m pytest -q` → **60 passed**
- `PYTHONPATH=src python -m coverage report -m` → **93% total coverage**, configured 90% gate satisfied
- `python -m compileall -q src tests` → **PASS**
- `PYTHONPATH=src python -m loopforge.entrypoints.cli demo` → **succeeded**, two iterations, externally verified stop
- architecture dependency tests run as part of pytest → **PASS**
- source/test line-length scan at 100 characters → **PASS**

Unavailable locally:
- Ruff
- Pyright
- Import Linter executable

Their configuration/CI gates remain present; this environment does not have those packages installed.

### Acceptance review

- strict tool contract semantics: satisfied
- model cannot downgrade its tool risk: satisfied
- versioned serialization skeleton: satisfied
- event catalog round-trip: satisfied
- invariant/property-style deterministic coverage: satisfied
- architecture gate: satisfied
- coverage gate: satisfied
- demo behavior preserved: satisfied
- documentation reconciled: satisfied

### Diff/repository review

Scope remains inside the v0.1 kernel contract. No SQLite store, retry scheduler, idempotency journal, circuit breaker, fault-injection lab, sandbox, or live-model adapter was implemented.

## Stop

Classification: SUCCESS

### Result

LoopForge now has a closed in-memory kernel authority/serialization contract suitable for a durable event store to implement next. Tool authority is code-owned, action authorization is auditable, event persistence has a versioned boundary, and deterministic checks meet the current repository coverage gate.

### Remaining issues

- Ruff/Pyright/Import Linter still require execution in a network-enabled/CI environment.
- Durable event storage and optimistic concurrency are intentionally absent.

### Candidate next cycles

- PACS-002 — Durable SQLite event store (`PLANNED` only)
- PACS-003 — Reliability control plane (`PLANNED` only)
