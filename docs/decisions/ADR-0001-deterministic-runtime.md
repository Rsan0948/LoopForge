# ADR-0001: Probabilistic model inside deterministic runtime

**Status:** Accepted

## Decision

The LLM is treated as a replaceable probabilistic decision component. Deterministic software owns permissions, state, tool execution, verification, resource limits, and terminal decisions.

## Consequences

- Model providers remain adapters.
- Tests can use scripted models.
- Reliability does not depend on prompt compliance for enforceable rules.
- Model autonomy is bounded by explicit capabilities.
