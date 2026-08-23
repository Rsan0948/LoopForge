# Agent Contribution Contract

These rules apply to humans and AI coding agents.

1. `loopforge.domain` must never import provider SDKs, persistence adapters, Git, HTTP, CLI, or telemetry implementations.
2. `loopforge.application` depends on abstractions in `ports`, never concrete adapters.
3. Authoritative run state changes only through domain events and the reducer.
4. Every registered external action must define code-owned risk, permission, side-effect, retry, idempotency, approval, timeout, and sensitivity metadata; model output may not define or downgrade these fields.
5. Any retryable side-effecting action must use natural or keyed idempotency before implementation.
6. Terminal run states cannot transition back to active states.
7. Model output crossing a system boundary must be validated by a strict schema.
8. Avoid `Any` in core packages. Provider-specific looseness stays quarantined in adapters.
9. No network access in unit tests.
10. A new control rule requires tests for allowed behavior and denial/failure behavior.
11. Security boundaries, hard budgets, legal transitions, and HITL requirements are immutable during a run.
12. Adaptive policy may optimize routing/context/parallelism, but may never expand runtime authority.

13. Sandbox capability requirements are code-owned workload contracts; unsupported isolation must fail before execution.
14. Repository/model content may never expand sandbox capabilities, command allowlists, environment exposure, or host permissions.
15. Do not describe a working directory, subprocess, timeout, or local adapter as strong isolation unless the adapter capability contract proves it.
