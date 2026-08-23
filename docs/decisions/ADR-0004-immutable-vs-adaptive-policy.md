# ADR-0004: Immutable authority vs adaptive execution policy

**Status:** Accepted

## Decision

Permissions, hard budgets, security boundaries, legal state transitions, and mandatory human gates are immutable during an execution. Future adaptive policies may optimize model routing, context allocation, worker count, and escalation strategy only within those boundaries.

> Adaptive systems may optimize within authority; they may not expand authority.
