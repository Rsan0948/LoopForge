# Manual PACS Development Process

LoopForge development is organized into manually initiated PACS cycles.

**PACS = Plan → Act → Check → Stop.**

The existence of a planned cycle does **not** authorize its execution. Every cycle must be explicitly initiated by the operator. A future cycle may be fully designed in advance and still remain inactive.

## Core rule

> Planned ≠ authorized.

Do not begin a subsequent PACS cycle automatically after completing the current one. Stop at the current cycle's terminal classification and wait for explicit operator initiation.

## Cycle states

A PACS cycle may be:

- **PLANNED** — scope exists, but no implementation work is authorized.
- **ACTIVE** — the operator explicitly initiated the cycle.
- **SUCCESS** — acceptance gate satisfied.
- **PARTIAL** — useful work completed, but the acceptance gate was not fully satisfied.
- **BLOCKED** — progress cannot continue without an external dependency, authority, or missing prerequisite.
- **FAILED** — the cycle's objective could not be achieved without redesign or rollback.

Only the operator changes a cycle from PLANNED to ACTIVE.

## Plan

Before implementation, record:

- cycle identifier and title;
- objective;
- why the cycle exists now;
- dependencies and prerequisites;
- explicit in-scope work;
- explicit out-of-scope work;
- architectural constraints that must remain true;
- acceptance gate;
- evidence required to classify the cycle SUCCESS.

The plan should be narrow enough that a cycle has one coherent architectural purpose.

## Act

Implement only the authorized scope.

During implementation:

- preserve the dependency DAG and architectural contracts;
- prefer deterministic enforcement over prompt-only conventions;
- do not silently expand authority or security boundaries;
- record architectural decisions when they affect future work;
- add tests with the behavior, especially invariants and failure modes;
- treat discovered defects in already-touched contracts as part of the cycle when leaving them unfixed would invalidate the acceptance gate;
- do not pull work from later cycles merely because it is convenient.

If later functionality must be anticipated, define a port, type, or contract without implementing the later capability.

## Check

Run every executable quality gate available in the environment and explicitly record any gate that could not be executed.

The standard check surface includes, as applicable:

- deterministic unit tests;
- invariant/property tests;
- integration tests;
- resilience/fault-injection tests;
- security tests;
- deterministic end-to-end demo;
- architecture dependency checks;
- compilation/build checks;
- formatting/lint/type checks when toolchains are available;
- coverage, especially branch coverage for control-policy code;
- diff/whitespace/line-length hygiene;
- documentation reconciliation;
- replay/restart behavior when persistence is involved.

A green test count alone is not sufficient. The cycle's stated acceptance gate must be demonstrated directly.

## Stop

Every ACTIVE cycle ends with exactly one terminal classification:

### SUCCESS

The acceptance gate is satisfied and supporting evidence is recorded.

### PARTIAL

The work is useful and internally consistent, but one or more acceptance criteria remain unmet. Record what remains and why.

### BLOCKED

An external prerequisite prevents completion. Record the blocker and the exact condition required to resume.

### FAILED

The approach did not meet the objective or would violate project invariants. Record the failure and recommended redesign/rollback.

After classification:

1. update the cycle record;
2. reconcile roadmap/build-status documentation;
3. commit the checkpoint;
4. stop.

**Do not start the next planned PACS cycle without explicit operator instruction.**

## Cycle record template

```markdown
# PACS-NNN — <title>

Status: PLANNED | ACTIVE | SUCCESS | PARTIAL | BLOCKED | FAILED

## Objective

...

## Why now

...

## Dependencies

...

## In scope

- ...

## Out of scope

- ...

## Plan

...

## Act

What was implemented and any relevant discoveries.

## Check

Commands/evidence/results.

## Stop

Final classification and rationale.

## Follow-on implications

What later cycles may build on this, without authorizing them.
```

## Operating philosophy

PACS is a project-development control mechanism, not an autonomous scheduler. It exists to keep architecture, implementation, verification, and stopping criteria explicit while preserving human authority over when the next development loop begins.
