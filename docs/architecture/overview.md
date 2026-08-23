# Architecture overview

LoopForge separates **authority** from **intelligence**.

The model may propose an action. It does not own permission, tool risk classification, retry/idempotency semantics, execution, state mutation, verification, budget enforcement, or terminal decisions.

## Dependency direction

```text
domain ← ports ← application ← entrypoints
   ↑        ↑                       │
   └── adapters ────────────────────┘
```

- `domain` contains provider-agnostic invariant-bearing types and reducers.
- `ports` define interfaces against the domain vocabulary.
- `application` coordinates ports and domain policy.
- `adapters` implement external concerns such as models, tools, persistence, and serialization.
- `entrypoints` are the composition root and may wire concrete adapters into the application runtime.

The dependency DAG is tested directly in `tests/architecture` and duplicated as an Import Linter CI contract when that development dependency is available.

## Authoritative history

Run history is represented as an append-only sequence of immutable events. `RunState` is a deterministic projection of those events. This creates a stable foundation for replay, later provenance graphs, counterfactual analysis, and debugging without hidden model reasoning.

The v0.1 event contract also defines a versioned JSON serialization boundary so durable persistence can be added without making storage responsible for domain serialization semantics. See `kernel-contract.md`.

## Tool authority

Tool authority is code-owned. A model proposal contains a tool name and arguments, while the registered tool contract owns risk, permission, side effects, retry class, idempotency, human approval, timeout, and data sensitivity. Unknown tools are rejected before execution.

## Control policy

Safety/authority policy is immutable during a run. Future adaptive policy may choose models, context budgets, or escalation strategies, but may not expand permissions or override hard limits.
