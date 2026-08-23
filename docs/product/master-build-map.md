# LoopForge master product build map

## Purpose

This document defines the end-state product map for LoopForge and the dependency order for building it. It sits above the version roadmap. The roadmap answers **what ships in each milestone**; this document answers **what the complete system is, how the major capability areas depend on one another, and what evidence proves each area is production-grade**.

LoopForge is a reference implementation and experimentation platform for bounded autonomous software-engineering agents. Its purpose is not to maximize apparent autonomy. Its purpose is to make model-driven execution typed, constrained, replayable, observable, fault-tolerant, and scientifically evaluable.

The reference workload is software repair because it naturally provides state, tools, real side effects, deterministic verification, branching, retries, isolation, measurable success, and human-approval boundaries.

## Product thesis

> A probabilistic model may optimize within explicitly granted authority, but the deterministic runtime owns authority, state, verification, side effects, budgets, recovery, and termination.

The long-term experimental thesis is:

> Execution history can improve adaptive policies such as routing, context allocation, escalation, and worker topology without allowing the model or learned policy to expand its own authority.

## Product success condition

LoopForge reaches a credible v1.0 when a reviewer can clone the repository, run deterministic checks locally, execute a bounded software-repair task with a live model, inspect the exact event/trace/provenance history, interrupt and resume the run, observe retry/idempotency behavior under injected faults, verify success independently of the model, compare multiple execution policies over a locked benchmark corpus, and reproduce the published benchmark report.

## System map

```text
                                    ┌──────────────────────────────┐
                                    │        HUMAN OPERATOR        │
                                    │ task • approval • cancel     │
                                    └──────────────┬───────────────┘
                                                   │
                                                   ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                             CONTROL PLANE                                     │
│                                                                               │
│ Immutable policy                    Adaptive execution policy                  │
│ ───────────────                    ─────────────────────────                  │
│ permissions                         model routing                              │
│ hard budgets                        context allocation                         │
│ legal transitions                   escalation timing                         │
│ sandbox boundaries                  worker topology                            │
│ HITL requirements                   evaluator selection                        │
│ secret/network policy               compaction thresholds                      │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                          EXECUTION RUNTIME                                    │
│                                                                               │
│ trigger → load state → build context → select model → propose action          │
│         → authorize → execute tool → observe → update state → verify          │
│         → continue / retry / reflect / escalate / approve / stop              │
└───────────────┬───────────────────────────────┬───────────────────────────────┘
                │                               │
                ▼                               ▼
       ┌──────────────────┐           ┌──────────────────────┐
       │    WORKSPACES    │           │      PROVIDERS       │
       │ sandbox/worktree │           │ models/tools/storage │
       └────────┬─────────┘           └──────────┬───────────┘
                │                                │
                └──────────────┬─────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                      AUTHORITATIVE EVENT HISTORY                              │
└───────────────┬──────────────────────┬───────────────────────┬────────────────┘
                ▼                      ▼                       ▼
            RunState               Telemetry              Provenance graph
                │                      │                       │
                └──────────────────────┴──────────────┬────────┘
                                                     ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                         EVALUATION LABORATORY                                 │
│ outcome • trajectory • cost • false success • resilience • policy comparison │
└───────────────────────────────────┬───────────────────────────────────────────┘
                                    ▼
                         candidate adaptive policies
                                    │
                      benchmark → shadow → review → promote
```

## Capability domains

### A. Runtime kernel

**Purpose:** define the invariant-bearing language of an agent run independently of providers and infrastructure.

Core capabilities:
- immutable domain events
- deterministic state projection
- legal state-transition enforcement
- typed identifiers and causal links
- terminal-state invariants
- explicit stop reasons
- model/action/observation/verifier vocabulary
- immutable vs adaptive policy boundary

Evidence of quality:
- deterministic replay produces identical state
- illegal transitions fail immediately
- terminal states cannot reactivate
- no provider SDK imports in domain code
- property tests exercise event sequences and invariants

**Status:** v0.1 kernel contract complete via PACS-001; durable storage remains a later capability.

---

### B. Ports, contracts, and provider isolation

**Purpose:** make models, tools, persistence, telemetry, sandboxing, approval, and verification replaceable implementations rather than architectural dependencies.

Core ports:
- `ModelPort`
- `ToolPort` / tool registry
- `StateStore`
- `Verifier`
- `SandboxPort`
- `WorkspaceManager`
- `ApprovalGateway`
- `TelemetryPort`
- `Clock`

Required contract metadata:
- strict input/output schemas
- provider capabilities
- side-effect class
- retry class
- idempotency semantics
- timeout policy
- permission requirement
- data sensitivity

Evidence of quality:
- adapters pass shared contract suites
- domain/application layers do not import concrete providers
- scripted/fake adapters can drive deterministic E2E tests

---

### C. Durable event store and concurrency model

**Purpose:** make runs resumable and causally reconstructable.

Core capabilities:
- versioned event serialization
- SQLite append-only event store for local reference implementation
- optimistic concurrency on stream version
- event schema evolution rules
- action journal
- recovery after process interruption
- snapshot/projection optimization only after replay correctness is established

Execution guarantee:
- at-least-once external execution unless an adapter explicitly provides stronger semantics
- duplicate side effects controlled by idempotency/deduplication at the external boundary

Evidence of quality:
- kill process mid-run and resume safely
- concurrent writers produce conflict rather than lost update
- old event fixtures remain replayable across schema changes

**Status:** PACS-002 completed the durable SQLite stream, compare-and-append concurrency, migration/versioning, and replay/resume foundation. PACS-003 adds the event-backed action journal, retry/idempotency semantics, and safe recovery from ambiguous idempotent side effects.

---

### D. Governance and reliability

**Purpose:** ensure autonomy remains bounded and operationally safe.

Core capabilities:
- hard iteration/time/token/USD limits
- pre-side-effect budget re-check
- retry classification
- exponential backoff + jitter
- idempotency keys/journal
- no-progress detection
- circuit breakers
- permission enforcement
- reason-coded control decisions
- cancellation

Control decision vocabulary should remain machine-readable, for example:
- `CONTINUE`
- `STOP_SUCCESS_VERIFIED`
- `STOP_BUDGET_EXHAUSTED`
- `STOP_MAX_ITERATIONS`
- `STOP_STALLED`
- `RETRY_TRANSIENT_FAILURE`
- `ESCALATE_STALLED`
- `REQUEST_HUMAN_APPROVAL`
- `BLOCK_PERMISSION_DENIED`

Evidence of quality:
- injected timeout after remote success does not duplicate a side effect
- hard limits cannot be overridden by model output
- a repeated non-improving trajectory is detected before max-iteration exhaustion
- every terminal run has an explicit reason code

**Status:** complete via PACS-003 for per-run retry/backoff/idempotency, circuit breaking, deterministic no-progress detection, token/time/USD/iteration governance, and cancellation. Systematic fault injection completed via PACS-004.

---

### E. Security and sandboxing

**Purpose:** assume both the model and the repository may produce unsafe requests/content.

Core capabilities:
- worker sandbox abstraction
- filesystem confinement
- resource/time/output limits
- command/tool allowlists
- network policy
- environment/secret filtering
- path traversal and symlink defense
- prompt-injection-aware trust metadata
- human gate for external/destructive writes
- secret redaction before telemetry emission

Trust classes should distinguish at minimum:
- runtime/system policy
- authorized human requirement
- deterministic environment observation
- retrieved external evidence
- model inference/reflection
- untrusted repository/web content

Evidence of quality:
- repository content cannot authorize privileged actions
- secrets are not surfaced into model context by default
- malicious fixture repos cannot escape a hardened sandbox in test scenarios

**Status:** PACS-005 establishes the security/trust vocabulary, sandbox port, capability negotiation,
and constrained local reference adapter. Strong hostile-code process filesystem/network/kernel
isolation remains a later container/VM adapter requirement and is not claimed by the local adapter.

---

### F. Context lifecycle

**Purpose:** make context a governed reasoning resource rather than an accumulated transcript.

Core capabilities:
- typed `ContextItem`
- provenance/trust/freshness metadata
- role-specific context builders
- compaction contracts
- pruning/supersession
- stable prompt prefixes
- prompt versioning
- cache telemetry
- adaptive context-budget allocation

Important invariants:
- authoritative observations cannot be silently overwritten by model inference
- compaction must preserve active objective, unresolved blockers, deterministic verifier failures, pending approvals, and irreversible actions already taken
- untrusted content cannot become policy authority merely because a model repeats it

Evidence of quality:
- snapshot tests show exact context sent to each role
- compaction tests prove required facts survive
- benchmark demonstrates token/context reduction without unacceptable success loss

---

### G. Software-repair reference workload

**Purpose:** provide a concrete workload that naturally exercises the runtime.

Core capabilities:
- repository ingest
- bounded read/search/edit tools
- Git diff/status tools
- isolated worktrees
- predefined command execution
- pytest/lint/type/build verifiers
- acceptance-test definition
- patch artifact
- optional PR creation behind HITL

Initial autonomy boundary:
- local repository operations allowed inside sandbox
- arbitrary host shell denied
- arbitrary internet denied by default
- production deployment denied
- external writes approval-gated

Evidence of quality:
- agent can fix a seeded bug and only succeed after external acceptance checks pass
- unrelated changes are detected/scored
- worker workspaces cannot silently interfere with one another

---

### H. Model integration and routing

**Purpose:** make model intelligence a measured, replaceable execution resource.

Core capabilities:
- provider-neutral request/response essentials
- provider-specific capability metadata
- structured action output
- usage/cost accounting
- vertical routing by capability tier
- horizontal fallback by provider/model
- escalation policy
- planner vs worker routing
- cache capability awareness

Evidence of quality:
- same deterministic workload can run against a scripted model and at least two live model configurations
- routing decisions are reason-coded and traceable
- fallback preserves task state

---

### I. Orchestration and HITL

**Purpose:** support complex decomposition without losing global control.

Core capabilities:
- orchestrator/worker split
- bounded worker briefs
- worker lifecycle
- parallel worktrees
- optimistic state concurrency
- evaluator/optimizer loop
- evidence-grounded Reflexion
- asynchronous `WAITING_FOR_APPROVAL`
- resume after approval/rejection

Evidence of quality:
- workers receive narrower role-specific context than orchestrator
- parallel execution cannot corrupt shared run state
- evaluator cannot override deterministic verifier failure
- a process can terminate while waiting for approval and resume later

---

### J. Observability and provenance

**Purpose:** make behavior explainable from observable execution rather than hidden model reasoning.

Telemetry layers:
- **events:** authoritative domain history
- **traces:** causal execution path
- **metrics:** aggregate behavior and economics
- **logs:** operational diagnostics

Core telemetry:
- run/cycle/worker/action/tool identifiers
- model selection + reason code
- prompt/template/context versions
- usage/cached token counts/cost
- action authorization
- tool latency/result
- retry/circuit behavior
- verifier outcome
- stop reason

Execution provenance graph:
- derive causal relationships among requirement, context evidence, hypotheses, actions, patches, observations, and verifier results
- preserve `caused_by` / `parent_event_id` / correlation relationships
- support queries such as `explain patch`, `why escalated`, and `what evidence supported this action`

Evidence of quality:
- a failed run can be reconstructed without chain-of-thought
- telemetry can correlate state → action → outcome
- provenance can distinguish chronological adjacency from causal dependency

---

### K. Evaluation laboratory

**Purpose:** evaluate agent systems scientifically rather than by anecdotal demos.

Benchmark principles:
- define/lock task corpus before optimization
- deterministic acceptance criteria
- multiple live-model trials per task
- deterministic CI tests separate from stochastic evals
- preserve benchmark/version metadata

Metrics:
- task success rate
- false-success rate
- cost per successful task
- median/p95 cost
- cycles/tool calls
- latency
- scope discipline
- context efficiency/cache ratio
- retries/escalations
- human interventions
- resilience under injected faults
- trajectory quality

Policy comparison:
- baseline frontier-only
- routed
- routed + compaction
- orchestrator/worker
- adaptive context allocation

Evidence of quality:
- benchmark report is reproducible from committed task definitions and run metadata
- improvements are measured across multiple trials rather than selected demos

---

### L. Adaptive experimentation layer

**Purpose:** explore genuinely new best practices while keeping authority deterministic.

V1 experimental features:
1. execution provenance graph
2. provenance/trust-aware context
3. adaptive context-budget allocation
4. trajectory-quality evaluation
5. shadow policy evaluation

Post-v1 candidates:
- learned model routing
- semantic progress detection
- candidate strategy extraction
- counterfactual replay branches requiring new inference

Promotion pipeline:

```text
execution history
    ↓
analysis / candidate adaptive policy
    ↓
locked benchmark
    ↓
shadow decisions
    ↓
canary/manual review if applicable
    ↓
promote versioned policy
```

Hard rule:

> Adaptive systems may improve how granted authority is used; they may not grant themselves new authority.

## Cross-cutting software-quality system

These standards apply to every capability domain.

### Architecture
- enforced acyclic dependency rules
- core/provider isolation
- ADR for material architectural changes
- generated dependency graph

### Types/contracts
- strict static typing in core/runtime
- no unbounded `Any` in core
- strict boundary validation
- versioned schemas

### Tests
- unit tests
- property/invariant tests
- architecture tests
- adapter contract tests
- integration tests
- fault/resilience tests
- deterministic E2E golden runs
- mutation testing on critical governance code
- live-model evals outside normal deterministic CI

### CI
Recommended gate order:

```text
lock consistency
→ formatting
→ lint
→ strict typecheck
→ architecture contracts
→ dependency/security audit
→ unit/property tests
→ integration tests
→ resilience tests
→ deterministic E2E
→ package build
```

### Documentation
- architecture overview
- ADRs
- threat model
- event catalog
- state machine
- tool-security model
- context lifecycle
- eval methodology
- AI-assisted engineering policy

### Release evidence
- test results
- coverage report
- dependency DAG
- state-machine diagram
- schemas/event catalog
- benchmark report when applicable
- SBOM/checksums

## Build dependency graph

This is the preferred construction order. Later work may be planned early, but implementation should not bypass required foundations.

```text
1. Domain vocabulary + invariants
   ↓
2. Events + deterministic projections + transitions
   ↓
3. Ports + strict contracts + fake adapters
   ↓
4. Durable store + serialization + concurrency
   ↓
5. Governance + retry/idempotency + fault injection
   ↓
6. Sandbox/security/tool capability model
   ↓
7. Context lifecycle + telemetry foundations
   ↓
8. Software-repair workload + deterministic verifiers
   ↓
9. First live model + structured actions
   ↓
10. Routing + planner/worker + HITL
   ↓
11. Provenance graph + rich observability
   ↓
12. Locked benchmark + multi-trial eval laboratory
   ↓
13. Adaptive context + shadow policies
   ↓
14. Post-v1 learned/semantic experimentation
```

## Product milestones and gates

### Milestone 0 — Runtime kernel

**Goal:** prove that LoopForge can model and execute bounded cycles deterministically without a live model.

Gate:
- event/state replay deterministic
- legal transitions enforced
- budget checked before side effects
- permission policy external to model
- scripted two-cycle run succeeds only after verifier pass
- architecture DAG enforced

**Current status:** complete via PACS-001.

### Milestone 1 — Durable reliability foundation

**Goal:** make runs resumable and side effects retry-safe.

Gate:
- versioned event serialization
- SQLite durable event stream
- optimistic concurrency
- retry/backoff/jitter
- idempotency journal
- no-progress detection
- circuit breaker
- fault-injection suite

### Milestone 2 — Secure execution environment

**Goal:** safely execute bounded real software tools.

Gate:
- sandbox abstraction + first hardened adapter
- tool metadata enforced
- filesystem/network/secrets policy
- threat-model fixtures
- repository cannot elevate authority through prompt injection

### Milestone 3 — Context + observability foundation

**Goal:** make every model decision inspectable from inputs and observable outcomes.

Gate:
- typed provenance-aware context
- compaction/pruning contracts
- prompt/template versioning
- structured logs
- traces + metrics
- context/cache/cost accounting

### Milestone 4 — Real software-repair agent

**Goal:** complete the first real model-driven task under deterministic control.

Gate:
- Git/worktree tooling
- deterministic pytest/lint/type/build verification
- first live model adapter
- structured action schema
- seeded bug fixture solved across repeated trials
- false model-declared completion rejected when checks fail

### Milestone 5 — Orchestration

**Goal:** support decomposed work and human approval without weakening control.

Gate:
- planner/worker split
- model capability routing
- bounded parallel workers
- evaluator + evidence-grounded reflection
- async HITL + resume
- fallback/escalation traced and reason-coded

### Milestone 6 — Provenance + evaluation laboratory

**Goal:** turn the runtime into an agent-systems research/evaluation platform.

Gate:
- execution provenance graph
- locked benchmark corpus
- multi-trial evaluator
- trajectory metrics
- false-success reporting
- reproducible benchmark report

### Milestone 7 — Adaptive execution experiments / v1.0

**Goal:** demonstrate safe runtime adaptation inside immutable authority.

Gate:
- adaptive context-budget policy
- shadow policy mechanism
- policy/version comparison
- documented benchmark evidence of at least one useful adaptive improvement
- immutable safety boundary remains mechanically enforced

## Manual PACS drill-down tree

PACS cycles are planning/execution units, not autonomous scheduling. Every cycle is initiated manually by the operator.

Recommended drill-down levels:

### Level 0 — Product
Example scope: "Advance LoopForge from v0.1 kernel to a credible v1.0 reference platform."

Output should establish:
- current product state
- highest-value next capability domain
- milestone dependencies
- broad acceptance gate

### Level 1 — Capability domain
Example scope: "Build durable reliability foundation."

Output should establish:
- architecture changes
- contracts/events required
- implementation slices
- test/evidence requirements
- risk/dependencies

### Level 2 — Feature/system
Example scope: "Implement SQLite event storage with optimistic concurrency."

Output should establish:
- exact interfaces
- data model
- failure semantics
- migrations/versioning
- tests
- docs

### Level 3 — Implementation slice
Example scope: "Implement append(stream_id, expected_version, events)."

Output should establish:
- exact code modifications
- edge cases
- unit/property/integration tests
- local acceptance commands

## Definition of a complete PACS cycle

A manually initiated PACS cycle is complete only when:

### Plan
- scope is explicit
- current state is inspected rather than assumed
- dependencies/invariants are identified
- acceptance evidence is defined before implementation

### Act
- implementation remains inside scope
- architectural contracts are preserved
- tests/docs/config are changed with production code where appropriate

### Check
- relevant deterministic checks actually run
- failures are diagnosed, not hidden
- acceptance evidence is compared against the original plan
- repository status/diff is reviewed

### Stop
The cycle ends with one explicit classification:
- `SUCCESS` — acceptance gate satisfied
- `PARTIAL` — useful progress but acceptance gate not fully satisfied
- `BLOCKED` — external constraint prevents completion
- `FAILED` — approach invalid or regression introduced

A successful cycle does **not** automatically initiate the next planned cycle. The operator starts the next cycle manually.

## Planned-cycle register

Future cycles may be planned in advance. Planning them does not authorize their execution.

Each planned cycle should record:
- cycle ID
- level (0–3)
- scope
- dependencies
- target milestone/capability
- intended acceptance gate
- status: `PLANNED | ACTIVE | COMPLETE | SUPERSEDED`

Only one cycle becomes `ACTIVE` when manually initiated unless the operator explicitly starts multiple parallel cycles.

## Immediate next recommended cycles from v0.1

The preferred sequence from the current repository state is:

1. **PACS-001 — Close the v0.1 kernel contract — COMPLETE**
   - tool metadata types
   - structured event serialization/versioning skeleton
   - strengthen invariants/property-style tests
   - verify current documentation matches implementation

2. **PACS-002 — Durable SQLite event store — COMPLETE**
   - append-only event stream
   - expected-version optimistic concurrency
   - replay/resume
   - migration/schema tests

3. **PACS-003 — Reliability control plane — COMPLETE**
   - retry classes
   - durable backoff/jitter
   - event-backed idempotency/action journal
   - circuit breaker
   - no-progress detector

4. **PACS-004 — Fault-injection laboratory — COMPLETE**
   - timeout
   - ambiguous remote success
   - duplicate event
   - process interruption
   - malformed model/tool result
   - budget exhaustion

5. **PACS-005 — Security + sandbox contract — COMPLETE**
   - threat model
   - capability metadata
   - sandbox port
   - first constrained execution adapter

The existence of PACS-002 through PACS-005 in this map does not initiate them.
