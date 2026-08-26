# LoopForge

LoopForge is a reference implementation and experimentation platform for **bounded autonomous software-engineering agents**.

Its core thesis is simple:

> A probabilistic model should operate inside a deterministic runtime that owns authority, state, verification, budgets, retries, and termination.

The coding-agent workload is a reference domain, not the product thesis. LoopForge exists to make agent execution **typed, replayable, observable, fault-tolerant, and scientifically evaluable**.

## Design principles

1. Models propose; the runtime enforces.
2. Environmental feedback beats prediction.
3. State is not conversation history.
4. Verification is independent of generation.
5. Every loop has finite resources.
6. High-risk side effects require explicit authority.
7. Adaptive policies can optimize within authority; they cannot expand authority.
8. Measure cost per completed task, not cost per inference.
9. Agent behavior should be explainable from state → action → outcome telemetry, not hidden reasoning.
10. Architecture is enforced by code and CI, not only documented.

## Current foundation: reliability + explicit sandbox/security contract

This first milestone deliberately contains **no live LLM integration**. It establishes the invariant-bearing core first:

- immutable domain events
- deterministic state projection
- explicit legal state transitions
- typed tool/model/state-store ports
- deterministic control policy
- budget and iteration governance
- code-owned tool authority metadata (risk, retry, idempotency, approval, timeout, sensitivity)
- model action proposals that cannot self-declare/downgrade authority
- an in-memory event store
- a durable SQLite append-only event store with optimistic concurrency
- safe replay/resume from persisted checkpoints
- event-backed action journal with durable attempt/idempotency semantics
- retry classification + bounded exponential backoff with deterministic jitter
- keyed/natural idempotent recovery from ambiguous tool outcomes
- per-run tool circuit breakers and deterministic no-progress detection
- USD/token/time/iteration budgets plus explicit cancellation
- a versioned JSON event serialization boundary
- a scripted model adapter for deterministic tests
- architecture contracts
- unit/property-style invariant tests
- deterministic fault-injection laboratory
- explicit trust/sandbox capability vocabulary
- constrained local sandbox reference adapter with fixed commands, filtered environment,
  file-API path/symlink defense, process timeout, resource limits, and bounded output
- typed, provenance-aware model context artifacts (`ContextItem`/`ModelContext`) with
  code-owned trust authority, guarded elevation, and a durable `ContextAssembled` record;
  the model boundary consumes `ModelContext`, never raw run state

A live model should be one of the later adapters, not the foundation of correctness. PACS-004 completed the systematic fault-injection laboratory. PACS-005 established the sandbox/security contract and first constrained local adapter. PACS-006 established the context authority model. That local adapter explicitly does **not** claim child-process filesystem, network, or kernel isolation; hostile repository execution still requires a later container/VM adapter.

## Architecture

```text
entrypoints
    ↓
application
    ↓
ports
    ↓
domain

adapters ──implement──> ports
```

The domain package cannot depend on provider SDKs, databases, Git, HTTP, CLI frameworks, or telemetry vendors.

See `docs/architecture/overview.md` and the ADRs for rationale. The full product capability/dependency map lives in `docs/product/master-build-map.md`; development PACS cycles are explicitly manual and documented in `docs/process/manual-pacs.md`.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
python -m loopforge.entrypoints.cli demo
```

The repository is designed to move to `uv` as the canonical environment manager once the first lockfile is generated in a network-enabled development environment.
