# ADR-0019: Scoped investigation and task preparation inside durable goals

Date: 2026-10-20  
Status: implemented and qualified for a bounded multi-file repository goal

## Decision

Extend the Phase 2 goal loop with `inspect`, `prepare`, `reproduce` and `retrieve`.
The supervisor may choose their order from the current legal actions. It is not
required to traverse the campaign map/bugs/boundaries/contracts/docs lanes.
Readiness, behavioral reproduction and acceptance are execution gates rather than
fixed reasoning stages.

`RepositoryGoalConfig` freezes the authorized revision, readable scope, editable
prefixes, trusted acceptance probes, branch, worker limits, and a catalog of
approved environments. Its `task` is an outer authority envelope; it has no
preselected changed-file deliverables. Environment catalog entries contain an
immutable image, fixed readiness commands and fixed project checks. The model
selects only a catalog ID. It cannot supply an image, executable, installer,
network permission, environment variable, limit or acceptance fixture.

After inspection, environment readiness and baseline reproduction, the supervisor
may propose an assignment objective, explicit files and supporting evidence IDs.
Code checks the files are within the original editable scope, checks source
evidence dependencies, and resolves the image, checks, probes and limits from
trusted policy. The resolved task narrows the original authority. Once assigned,
its environment and task contract cannot be replaced by another proposal.

The authorized worker objective remains the exact operator goal. Supervisor
assignment/recovery guidance enters the worker as a separate `MODEL_INFERENCE`
context item, with model provenance. It is never relabelled authorized-human or
runtime-policy input. File scope becomes runtime policy only after the explicit
code-owned narrowing check against the frozen authorization and evidence basis.
Fresh review and candidate delivery retain the Phase 1/2 receipt requirements.

## Evidence and durable memory

Inspection reads regular text blobs from the pinned Git revision, within the
configured scope. It supports directory inventory, bounded source ranges and
literal queries. Parent traversal, hidden paths, excluded credential paths,
symlinks, submodules and unscoped paths cannot become source reads. Inspection
never interprets repository instructions as runtime policy.

Each measurement records its operation key, content identity, revision,
reference/range/query, bounded excerpt, omission flag and typed failure category.
Original content and immutable operation receipts live in a content-addressed
SQLite evidence store. Goal state changes only through `ResearchRecorded` events
and the reducer. The reducer checks the checkpoint, proposed operation, approved
environment and assignment lifecycle. Replaying a lost acknowledgement reuses
the exact receipt instead of replacing the observation.

The current plan and all decision revisions remain in the goal journal. The
provider receives the current plan, recent bounded evidence, a compact catalog,
all evidence IDs, and explicit IDs/counts for omitted memory. It can retrieve
original evidence owned by this goal, including later line ranges. Observations
and model reasoning remain lower-trust content. This is explicit evidence memory,
not a claim that summaries are facts or that every old hypothesis remains in
model context. Source artifacts are limited to 256 KiB, source/measurement
excerpts to 8 KiB, and the existing provider request admission limit still applies.
Large/minified artifacts can require further decomposition or a specific blocker.

Repeated measurements are compared by evidence content and excerpt, rather than
by tool-call spelling or query changes. A hard meaningful-progress allowance and
research-failure allowance stop repeated paid loops. All decisions, retrievals
and recovery calls still consume the original goal decision/time/cost limits.

## Environment preparation and bounded recovery

Preparation materializes a separate baseline checkout and measures an approved
installed image using fixed readiness commands. Reproduction records project
checks and independent behavioral probes on that original checkpoint. Project
baseline failures remain distinct from acceptance failures. A reproduced goal
requires an actually executed, nonzero independent behavioral failure; an
unexecuted check or a zero-exit program that skips the completion marker cannot
establish reproduction.

Commands run in the existing container adapter with its capability contract
required before execution: read-only workspace and root filesystem, denied
network, bounded resources/output/time and filtered environment. There is no
image pull, host fallback or arbitrary dependency installation. The baseline
checkout must remain unchanged before and after measurement. Working-tree
separation itself is not an isolation guarantee.

An unavailable dependency/runtime is recorded as environment evidence. The
supervisor may choose another originally approved environment or stop with a
blocker. No hidden permission request or authority expansion is inferred from
failure. After assignment, recovery preserves the frozen worker contract and
creates bounded successor runs; unsupported environment changes must block.

Worker failure classification uses recorded model/check/stop events: provider,
context admission, no progress, verification, environment, policy allowance or
unknown. Prose completion claims cannot set the category. The supervisor can
retrieve evidence, revise its plan, retry inside the remaining contract, or
block. Current supervisor plans and rationales are passed as lower-trust recovery
guidance; they do not change checks or limits. The single-worker goal loop
processes ready coding/review results without external chat acknowledgements.

## Qualification

Network-free tests cover scoped/pinned reads, directory inventory, excluded
paths, original retrieval, explicit omissions, stable lost-ack receipts,
environment and reproduction gates, invalid/unexecuted evidence, immutable
artifacts, assignment scope denial, unknown authority fields, intrinsic guidance
trust, meaningful progress and typed failure classification. Real-container tests
complete a discovered two-file task with an initially unavailable dependency and
a restart after baseline reproduction.

The live evaluation shared only a generated quantity-summary fixture. The first
trial stopped at a directory-inspection mismatch; the directory inventory fix is
recorded as implementation intervention and that terminal goal was preserved.
A separate clean trial:

1. Inspected the source and discovered the shared normalizer and report code.
2. Recovered from an explicitly injected transient container failure and an
   unavailable pytest dependency, selecting the approved standard-library setup.
3. Reproduced the independent behavioral failures on the original revision.
4. Restarted composition with discoveries, environment and reproduction intact.
5. Proposed a two-file assignment without operator-supplied edit locations.
6. Recovered from an invalid local worker response by a bounded successor.
7. Passed fresh review, project checks and both independent acceptance criteria,
   then delivered a candidate without chat intervention or harness repair during
   that clean run.

The original repository stayed unchanged. The candidate remains `patch_ready`;
no local acceptance, push, merge, deployment or production migration occurred.
One clean synthetic task establishes this bounded gate, not general unattended
reliability. Representative repeated evaluation and overnight rollout remain
later phases.

Phase 3 usage-based charges were $0.004400 (blocked trial) and $0.088800 (clean
trial). With Phase 2's $0.021008, total conservative accounting is **$0.114208 of
$5 authorized**. These are token-usage estimates, not a verified account bill.
Both Phase 3 ledgers have fixed $0.75 ceilings; together with Phase 2's two $0.50
ceilings, maximum preallocated evaluation authority is $2.50. Rerunning a ledger
cannot reset its spending, deadline or terminal status.

## Scope limits

This phase supports one narrowed assignment and one active local worker, approved
installed environments, and independently specified behavioral probes. It does
not build arbitrary images, install dependencies over the network, discover new
authority, or solve every environment automatically. It starts from an explicit
commit; dirty snapshots, clarification deadlines, experiment lineage, late user
answers and goal UI controls are Phase 4. Production repository sharing and
rollout remain unconfigured.
