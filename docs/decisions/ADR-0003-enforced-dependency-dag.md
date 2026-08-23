# ADR-0003: Enforced dependency DAG

**Status:** Accepted

## Decision

Architecture boundaries are checked automatically. Domain code cannot depend on entrypoints, concrete adapters, or provider-specific infrastructure.

## Why

The project should prove its architecture, not merely describe it.
