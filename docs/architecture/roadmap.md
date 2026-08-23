# Engineering roadmap

This is the concise release roadmap. See `docs/product/master-build-map.md` for the end-state capability map, dependency graph, milestone acceptance gates, and planned manual PACS drill-down structure. See `docs/process/manual-pacs.md` for the development-cycle operating procedure.

## v0.1 — Runtime kernel contract

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

## v0.2 — Durable reliability foundation

- [x] SQLite event store with expected-version optimistic concurrency
- [x] durable replay/resume with fail-closed ambiguous-action recovery
- [x] retry/backoff/jitter policy
- [x] event-backed idempotency/action journal
- [x] no-progress detector
- [x] circuit breakers
- [x] fault-injection suite

**Status:** durable persistence complete via PACS-002 and reliability control plane complete via PACS-003. Fault-injection laboratory completed via PACS-004.

## v0.3 — Context + observability

- typed context items with provenance/trust metadata
- context builder + compaction contracts
- prompt versioning/cache telemetry
- OpenTelemetry traces/metrics
- execution provenance graph projection

## v0.4 — Software-repair workload

- [x] sandbox port + constrained local reference adapter (PACS-005)
- hardened container/VM adapter with process filesystem + network isolation
- Git/worktree manager
- pytest/lint/type/build verifiers
- bounded filesystem/search/edit tools
- first live model adapter

## v0.5 — Orchestration

- planner/worker split
- model capability registry + vertical/horizontal routing
- evaluator/optimizer
- Reflexion with evidence provenance
- asynchronous HITL state

## v1.0 — Evaluation laboratory

- locked benchmark corpus before optimization
- multi-trial live-model evals
- false-success rate
- trajectory-quality metrics
- adaptive context-budget policy
- shadow policies + counterfactual replay
- reproducible benchmark report
