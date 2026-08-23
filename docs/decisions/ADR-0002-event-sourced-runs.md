# ADR-0002: Event-sourced authoritative run history

**Status:** Accepted

## Decision

Important workflow changes are immutable domain events. Current run state is projected by a deterministic reducer.

## Why

This enables replay, causal/provenance projections, optimistic concurrency, exact debugging, and later counterfactual policy experiments.

## Constraint

Operational logs are not authoritative state.
