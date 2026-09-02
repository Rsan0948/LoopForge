# Remaining PACS Plan — v1.0

Status: **PLANNED ONLY**

This document decomposes the remaining LoopForge v1.0 work after PACS-001 through PACS-005. None of the cycles below are active merely because they are specified here.

> **Planned ≠ authorized. The operator must manually initiate every PACS cycle.**

> **Standing gate (operator decision, 2026-09-02): every cycle keeps the
> operator console current.** Any cycle that adds or changes operator-visible
> capability must surface it in the console (REST + React UI) in the same
> cycle — or record an explicit operator decision for why no UI change is
> warranted. The console must never fall behind the engine it drives. This
> is repeated in each remaining cycle's acceptance gate below.

The dependency order is intentional:

```text
context authority
→ context lifecycle
→ observability
→ hardened isolation
→ software-repair workload
→ live models
→ routing
→ orchestration/HITL
→ provenance
→ evals
→ adaptive execution
```

## PACS-006 — Context authority model

**Objective:** Make model context a first-class, typed, provenance-aware runtime artifact rather than an ad hoc collection of strings.

**Dependencies:** PACS-001–005.

**Build:**

- `ContextItem` / `ModelContext` domain vocabulary;
- explicit source/provenance metadata;
- trust/authority classes separating runtime policy, authorized human requirements, deterministic observations, retrieved material, model inference, and untrusted content;
- supersession and freshness semantics;
- sensitivity metadata compatible with telemetry redaction;
- `ContextBuilderPort` boundary;
- rules preventing lower-trust content from silently becoming higher-authority policy.

**Acceptance gate:**

- context objects are typed and immutable at the model boundary;
- provenance/trust survive serialization/replay where required;
- untrusted content cannot independently authorize a privileged action;
- deterministic tests cover authority ordering, supersession, and invalid elevation attempts.

**Out of scope:** compaction optimization, prompt caching, live models.

---

## PACS-007 — Context lifecycle and prompt contracts

**Objective:** Build deterministic context selection, token budgeting, compaction/pruning, and versioned prompt artifacts on top of PACS-006.

**Dependencies:** PACS-006.

**Build:**

- `ContextBuilder.build(state, role, token_budget)`;
- explicit context-budget accounting;
- preservation contracts for objective, blockers, latest verifier failures, externally confirmed facts, pending approvals, and irreversible actions;
- structured compaction/pruning;
- role-specific context assembly;
- versioned prompt templates/artifacts;
- stable-prefix layout that is cache-friendly without coupling core semantics to one provider;
- snapshot/invariant tests for context construction.

**Acceptance gate:**

- model context deterministically fits the requested budget or fails explicitly;
- compaction never drops required preserved facts;
- prompt/template versions are captured as execution metadata;
- equivalent state + policy yields equivalent context construction;
- tests prove high-signal context survives repeated compaction.

**Out of scope:** provider-specific prompt-cache implementation.

---

## PACS-008 — Observability foundation

**Objective:** Make every important runtime decision explainable operationally without treating logs as authoritative state.

**Dependencies:** PACS-006/007 preferred; PACS-001–005 required.

**Build:**

- telemetry port and structured event logging;
- run/worker/cycle/action/tool/verification correlation identifiers;
- OpenTelemetry-compatible traces and metrics;
- spans for context build, model boundary, policy decision, tool execution, verifier, persistence, retry, and sandbox execution;
- token/cost/cache accounting fields ready for live models;
- redaction before telemetry emission;
- metrics for runs, duration, success, cycles, retries, circuits, stalls, budget stops, tool failures, verification failures, context size/compaction, and approvals.

**Acceptance gate:**

- a deterministic demo produces a causally correlated trace/log narrative;
- secrets/sensitive fields are redacted before export;
- event store remains the authoritative history and telemetry is explicitly non-authoritative;
- observability failures cannot corrupt run state.

---

## PACS-009 — Hardened container sandbox

**Objective:** Add an isolation backend suitable for untrusted repository/build execution and enforce capabilities rather than merely documenting them.

**Dependencies:** PACS-005.

**Build:**

- container-backed `SandboxPort` adapter;
- filesystem/process/network isolation capabilities;
- explicit network policy;
- resource limits and process cleanup;
- environment/secret filtering;
- path/symlink protections retained from local adapter;
- bounded output/timeouts;
- capability negotiation tests demonstrating fail-closed binding.

**Acceptance gate:**

- hostile test workloads cannot escape the configured workspace boundary;
- workloads requiring network isolation cannot bind to adapters that lack it;
- process timeout/resource-limit behavior is demonstrated externally, not simulated;
- sandbox destruction leaves no running child workload;
- security test suite covers traversal, symlinks, environment leakage, process/resource abuse, and capability mismatch.

**Out of scope:** claiming VM/kernel-grade isolation beyond what the selected backend actually provides.

---

## PACS-010 — Software-repair workload and deterministic verifier stack

**Objective:** Turn software repair into the reference workload while keeping the runtime itself workload-agnostic.

**Dependencies:** PACS-007, PACS-009.

**Build:**

- repository/workspace manager port;
- Git status/diff/checkout primitives;
- safe file read/search/edit tools;
- predefined test/lint/type/build commands through the sandbox;
- deterministic verifier composition;
- acceptance-criteria verifier hooks;
- workspace snapshot/diff artifacts;
- fixture repositories for deterministic repair tasks.

**Acceptance gate:**

- a scripted model can repair a fixture repository through the full runtime;
- success is granted only after independent verifier approval;
- false-success attempts are rejected;
- repository changes remain confined to the assigned workspace;
- a replayable run captures the exact patch and verification evidence.

---

## PACS-011 — First live model adapter

**Objective:** Integrate one real model provider without giving the provider ownership of LoopForge's control loop, state, permissions, or stopping decisions.

**Dependencies:** PACS-007, PACS-008, PACS-010.

**Build:**

- one production-quality `ModelPort` adapter;
- structured action/output validation;
- token/cost/latency accounting;
- provider failure normalization;
- tool proposal parsing;
- model capability metadata;
- secret isolation so credentials never enter model context;
- live-model tests/evals separated from deterministic CI.

**Acceptance gate:**

- a live model completes at least one software-repair fixture through the existing deterministic runtime;
- malformed provider responses fail explicitly and safely;
- model/provider errors map to runtime failure classes without leaking provider semantics into domain code;
- deterministic CI remains runnable with zero provider credentials.

---

## PACS-012 — Model capability registry and routing

**Objective:** Route by required capabilities, task/risk characteristics, execution state, and budget rather than hard-coded provider names.

**Dependencies:** PACS-011.

**Build:**

- provider/model capability registry;
- model-tier vocabulary;
- vertical routing between model strengths/cost classes;
- horizontal provider fallback;
- reason-coded routing decisions;
- escalation policy based on stalls/failures/complexity/budget;
- routing telemetry and deterministic policy tests.

**Acceptance gate:**

- routing policy can be tested entirely with fake adapters;
- a task requiring an unsupported capability cannot be routed to an incompatible model;
- every route/escalation emits a machine-readable reason code;
- hard budgets and permission policy remain outside adaptive routing authority.

---

## PACS-013 — Orchestrator/worker and worktree isolation

**Objective:** Add bounded multi-agent execution only where decomposition provides measurable value.

**Dependencies:** PACS-010, PACS-012.

**Build:**

- orchestrator/worker contracts;
- worker-scoped context/state projections;
- Git worktree workspace isolation;
- worker ownership and lifecycle events;
- optimistic concurrency for shared run state;
- deterministic merge/reconciliation policy;
- bounded worker counts and budget partitioning.

**Acceptance gate:**

- two workers can operate concurrently without sharing a mutable filesystem workspace;
- conflicting state updates are rejected/reconciled explicitly;
- global budgets remain enforceable across workers;
- orchestrator owns the global plan while workers own assigned workspaces;
- multi-agent execution provides a benchmarkable path, not an always-on default.

---

## PACS-014 — Evaluator, evidence-grounded Reflexion, and asynchronous HITL

**Objective:** Add independent evaluation, structured reflection, and durable human approval pauses without exposing hidden chain-of-thought as a system requirement.

**Dependencies:** PACS-013.

**Build:**

- evaluator-optimizer loop;
- structured reflections grounded in observable failure evidence;
- reflection persistence/versioning;
- approval gateway port;
- durable `WAITING_FOR_APPROVAL` state and resume path;
- approval policies for high-risk/external writes;
- explicit approval grant/reject events.

**Acceptance gate:**

- evaluator disagreement can prevent model-declared success;
- reflection records reference observable artifacts/verifier failures rather than hidden reasoning;
- a run can pause, process-restart, receive approval, and resume safely;
- approval cannot expand authority beyond immutable runtime policy.

---

## PACS-015 — Execution provenance graph and trajectory debugger

**Objective:** Derive causal execution provenance from the authoritative event stream so LoopForge can answer *why* an artifact/action exists, not merely what happened before it.

**Dependencies:** PACS-008, PACS-010, PACS-014.

**Build:**

- derived provenance DAG projection;
- typed nodes/edges for requirements, context evidence, observations, hypotheses/reflections, actions, patches, tool results, and verifier outcomes;
- causal/parent/correlation relationships;
- `explain` query surface such as “why did patch X happen?”;
- trajectory debugger based on observable artifacts;
- provenance-aware links back into context items.

**Acceptance gate:**

- event log remains authoritative and the provenance graph can be rebuilt from it;
- every material patch can be traced to triggering evidence/actions and subsequent verification;
- provenance never fabricates hidden chain-of-thought;
- graph reconstruction is deterministic for the same event stream;
- the console exposes the provenance/`explain` surface for operator sessions (standing UI gate).

---

## PACS-016 — Locked benchmark and multi-trial evaluation laboratory

**Objective:** Evaluate runtime policy scientifically rather than judging success from demos or one-off model runs.

**Dependencies:** PACS-010–015.

**Build:**

- locked benchmark fixture set including simple bugs, multi-file changes, misleading failures, transient APIs, ambiguous success, context pollution, stale state, stalls, prompt injection, HITL, parallel work, and provider outage;
- task/trial/grader/transcript model;
- multi-trial live-model eval runner;
- deterministic graders and explicit false-success metric;
- trajectory-quality metrics: repetition, unnecessary expensive-model use, scope discipline, permission requests, context efficiency, recovery behavior;
- success/cost/latency/human-intervention Pareto reports;
- benchmark versioning.

**Acceptance gate:**

- benchmarks are defined independently of whichever policy is being optimized;
- multiple trials produce aggregate nondeterministic metrics;
- false-success rate is first-class;
- deterministic tests and live-model evals remain separate;
- benchmark reports can compare runtime configurations, not merely model brands;
- the console exposes benchmark/trial results for operator inspection (standing UI gate).

---

## PACS-017 — Adaptive context and shadow policies / v1.0

**Objective:** Demonstrate bounded adaptation from execution history while preserving immutable runtime authority.

**Dependencies:** PACS-015, PACS-016.

**Build:**

- adaptive context-budget allocator;
- candidate execution-policy versions;
- shadow-policy evaluation: active policy executes while candidate decisions are recorded but not enacted;
- counterfactual replay from historical event state where deterministic replay is possible;
- policy comparison against benchmark/Pareto metrics;
- controlled promotion workflow;
- optional statistical routing/context heuristics based on collected evidence.

**Immutable during runs:**

- permissions;
- security boundaries;
- legal state transitions;
- hard budgets;
- HITL requirements;
- secret handling;
- authority-expansion rules.

**Adaptive within those boundaries:**

- model selection;
- context allocation;
- escalation timing;
- evaluator selection;
- worker count;
- compaction thresholds;
- execution strategy.

**Acceptance gate / v1.0:**

- an adaptive candidate can be benchmarked and shadowed without changing active-run authority;
- candidate policy cannot self-promote;
- counterfactual/shadow results are auditable through provenance and telemetry;
- deterministic safety policy remains immutable during execution;
- v1 benchmark/report demonstrates quality, cost, latency, context, recovery, and human-intervention tradeoffs across at least two execution policies/model configurations;
- documentation and release artifacts describe limits honestly;
- the console exposes shadow-policy/candidate comparisons (or an explicit operator decision records why no UI change is warranted — standing UI gate).

---

# v1.0 completion standard

PACS-017 is not considered sufficient by itself. v1.0 requires all earlier cycle gates to remain green and should provide, at minimum:

- bounded/replayable event-sourced runtime;
- persistent state with optimistic concurrency;
- retries, idempotency, circuit breaking, budgets, cancellation, and stall detection;
- explicit security/tool/sandbox capability contracts;
- provenance-aware context and deterministic compaction;
- structured telemetry;
- isolated software-repair workload;
- live provider support without provider-owned control flow;
- routing and bounded orchestration;
- independent verification, Reflexion artifacts, and durable HITL;
- execution provenance graph;
- locked multi-trial benchmark/eval laboratory;
- shadow/adaptive execution policy that cannot modify immutable authority;
- documented architecture, ADRs, threat model, benchmark methodology, and limitations;
- reproducible developer checks and a clean release build.

The intended thesis demonstrated by v1.0 is:

> **AI contributes intelligence; engineered systems provide reliability. Adaptive execution may optimize within authority, but it cannot expand its authority.**
