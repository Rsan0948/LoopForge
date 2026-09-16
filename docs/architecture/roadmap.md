# Engineering roadmap

This is the concise architecture roadmap. Its milestone labels describe the
reference-architecture program and are independent of the installable package's
Semantic Versioning. The package remains in the `0.2.x` public-beta line. See
`docs/product/master-build-map.md` for the capability map and acceptance gates,
and `docs/process/manual-pacs.md` for the development-cycle procedure.

## Architecture milestone 0.1 — Runtime kernel contract

- [x] immutable events
- [x] deterministic state projection
- [x] legal-transition enforcement
- [x] provider-independent ports
- [x] deterministic control/permission policies
- [x] scripted model/tool adapters
- [x] bounded model-cost semantics
- [x] deterministic unit/E2E-style demo
- [x] code-owned tool metadata: risk/retry/idempotency/approval/timeout/sensitivity
- [x] versioned event serialization boundary + JSON reference codec
- [x] architecture/invariant/property-style kernel tests

**Status:** complete via PACS-001. Durable storage is intentionally deferred to PACS-002 rather than being part of the in-memory kernel contract.

## Architecture milestone 0.2 — Durable reliability foundation

- [x] SQLite event store with expected-version optimistic concurrency
- [x] durable replay/resume with fail-closed ambiguous-action recovery
- [x] retry/backoff/jitter policy
- [x] event-backed idempotency/action journal
- [x] no-progress detector
- [x] circuit breakers
- [x] fault-injection suite

**Status:** durable persistence complete via PACS-002 and reliability control plane complete via PACS-003. Fault-injection laboratory completed via PACS-004.

## Architecture milestone 0.3 — Context + observability

- [x] typed context items with provenance/trust metadata (PACS-006)
- [x] context builder boundary + durable context assembly record (PACS-006)
- [x] context selection/compaction contracts and prompt versioning (PACS-007)
- [x] OpenTelemetry-compatible traces/metrics foundation (PACS-008)
- [x] execution provenance graph projection (PACS-015)

**Status:** complete via PACS-006 through PACS-008 and PACS-015.

## Architecture milestone 0.4 — Software-repair workload

- [x] sandbox port + constrained local reference adapter (PACS-005)
- [x] hardened container adapter with process-filesystem + network isolation
- [x] Git/worktree manager
- [x] pytest/lint/type/build verifiers
- [x] bounded filesystem/search/edit tools
- [x] first live model adapter

**Status:** complete via PACS-009 through PACS-011.

## Architecture milestone 0.5 — Orchestration and operations

- [x] planner/worker split and worktree isolation
- [x] model capability registry + deterministic routing
- [x] operator command center with durable sessions
- [x] human approval, intervention, and follow-up state
- [x] execution provenance and explain chains

**Status:** complete via PACS-012 through PACS-015.

## Reference-architecture milestone 1.0 — Evaluation laboratory

- [x] locked benchmark corpus before optimization
- [x] multi-trial live-model evals
- [x] false-success as a first-class verdict
- [x] trajectory-quality metrics
- [x] adaptive context-budget policy
- [x] shadow policies + counterfactual replay
- [x] reproducible benchmark reports and Pareto comparisons

**Status:** reference-architecture program complete via PACS-016 and PACS-017;
the distributable remains a public beta rather than a production-maturity claim.
