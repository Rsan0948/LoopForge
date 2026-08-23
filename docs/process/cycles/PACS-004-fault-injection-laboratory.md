# PACS-004 — Fault-injection laboratory

Level: capability
Status: COMPLETE
Parent: durable reliability foundation
Target milestone: v0.2

## Plan

### Current state
PACS-003 provides durable replay, runtime-owned retry/backoff/idempotency, circuit breaking,
no-progress detection, resource governance, and explicit cancellation. Existing tests cover many
individual reliability paths, but there is not yet a named, reusable fault-injection laboratory
that exercises the reliability contract as a system.

### Objective
Create a systematic, deterministic fault-injection layer and resilience suite covering timeout,
ambiguous remote success, duplicate event delivery, process interruption, malformed provider
results, and budget exhaustion. Harden provider result boundaries where fault injection exposes
untyped failure modes.

### Invariants / constraints
- Fault injection must be deterministic and reproducible.
- Model output cannot change retry/idempotency/tool authority.
- Ambiguous non-idempotent side effects remain fail-closed.
- The lab must not introduce a live model dependency.
- Provider contract failures must not corrupt durable run state.
- PACS-005 sandbox/security work is out of scope.

### Acceptance evidence
- named resilience/fault matrix exists in repository documentation
- timeout fault is retried according to runtime policy
- ambiguous remote success produces exactly one side effect under keyed idempotency
- duplicate event delivery is rejected by durable stores
- process interruption after side effect can resume safely only when idempotency permits
- malformed model and malformed tool results fail with explicit boundary errors
- budget exhaustion stops before unauthorized additional side effects
- full deterministic test/coverage/architecture/compile/demo checks remain green

### Expected files/systems touched
- provider ports/runtime boundary validation
- deterministic fault adapters
- resilience tests and fault-matrix documentation
- build/roadmap/PACS status documentation

### Risks
- fault adapters becoming production behavior rather than test infrastructure
- accidentally weakening strict types to permit malformed fixtures
- conflating simulated timeouts with OS-level hard process cancellation

## Act

- Added reusable deterministic `FaultInjectingTools` with transient-timeout,
  ambiguous-after-success, and crash-after-success injection points.
- Added explicit model/tool adapter response validation at the runtime boundary.
- Added a seven-case resilience matrix exercising reliability behavior end-to-end.
- Kept retry authority in the runtime; fault adapters only report operational observations.
- Removed SQLite test connection warnings surfaced during the acceptance sweep.

## Check

### Checks executed
- `PYTHONPATH=src python -m pytest -q` — 103 passing
- `PYTHONPATH=src python -m pytest --cov=loopforge --cov-branch --cov-report=term-missing -q`
  — 92% overall coverage, 90% gate satisfied
- `python -m compileall -q src tests` — passing
- deterministic CLI demo — passing
- architecture DAG suite — passing as part of pytest
- `git diff --check` — passing
- 100-character source/test line scan — passing

### Acceptance review
All planned faults have deterministic evidence. Malformed provider results fail at explicit typed
boundaries. Ambiguous keyed writes and restart recovery preserve exactly one logical side effect.
Hard budget policy prevents the downstream action from executing.

### Diff/repository review
Changes remain limited to fault infrastructure, provider-boundary hardening, resilience tests, and
associated documentation. No PACS-005 sandbox implementation was started.

## Stop

Classification: SUCCESS

### Result
LoopForge now has a repeatable adversarial reliability laboratory rather than reliability claims
spread across isolated tests. Provider contract corruption also fails explicitly and durably.

### Remaining issues
- OS/process sandbox enforcement is intentionally deferred to PACS-005.
- Live-provider/network chaos remains a later eval/integration concern.

### Candidate next cycles
- PACS-005 — Security + sandbox contract (planned; not active until manually initiated).
