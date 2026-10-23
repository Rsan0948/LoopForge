# ADR-0016: Distinguish passing checkpoints from task completion

Date: 2026-10-20
Status: accepted for the runtime foundation; supervisor workload wiring pending

## Context

The existing runtime treats every passing verification as whole-task success.
An adaptive supervisor needs to run useful checks during a multi-part task
without terminating work at the first green checkpoint. A worker's statement
that work is complete cannot supply the acceptance verdict.

## Decision

The code-owned verifier returns `VerificationResult.task_complete`. A passing
result with `task_complete=False` records `VerificationPassed` with the same
flag, projects a nonterminal reflection state, and continues within the original
budgets. Only a passing whole-task verdict permits `STOP_SUCCESS_VERIFIED`.
Failing checks cannot grant success, regardless of the completion flag.

The event is authoritative. Replay, store indexes, counterfactual extraction,
telemetry, provenance, the server response and console preserve or expose the
distinction. Repeated passing checkpoints remain subject to no-progress and
iteration/time/token/cost controls. Checkpoint events retain verifier-supplied
progress scores, so distinct verified progress does not count as a repeated green
checkpoint. Historical events retain their default score of 1.0. The fixed-roster
orchestrator cannot invent additional worker obligations: an incomplete final integration verdict stops
with `MERGED_TASK_INCOMPLETE` instead of accepting the result.

The new boolean is strictly validated at verifier, event and JSON boundaries.
Missing fields in historical events default to whole-task verification, retaining
schema-v1 replay semantics. Passing checkpoints use a schema-v2 envelope;
older readers reject it instead of ignoring the new flag and turning a checkpoint
into task completion. Schema v2 is restricted to explicit incomplete passing
verifications; ordinary events continue using v1. Existing workload verifiers
retain their previous whole-task interpretation. This is an additive runtime capability, not a claim
that legacy campaign checks now assess arbitrary acceptance criteria.

## Consequences and remaining work

The supervisor workload must explicitly assess all authorized acceptance criteria
and return a checkpoint verdict until they are satisfied. Its evidence must be
tied to the actual candidate identity and independent acceptance fixtures.
This ADR does not authorize automatic campaign acceptance or remote delivery.

A durable SQLite regression runs two task parts across a runtime restart and
proves that the first passing checkpoint neither terminates the run nor loses
its meaning during replay. Denial tests cover repeated checkpoints, exhaustion,
failed subsequent acceptance and malformed completion flags.


## Acceptance and diagnostic hardening

The success grader requires the latest verification to be a complete passing
verdict for the same run, preceding its stop. Final acceptance-hook evidence
comes only from the latest complete passing verification. Checkpoints and
superseded receipts cannot certify the final candidate.

Inconclusive failure flags survive orchestration and counterfactual extraction;
the API shows the latest verdict's flag rather than an older failure's flag.
Verifier inputs strictly validate both boolean flags and numeric progress scores.
These checks supplement the workload's future revision-bound acceptance contract;
they do not supply that contract themselves.
