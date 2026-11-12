# Campaign harness reliability and useful-throughput plan

Status: implementation and gated qualification authorized by the user ("Go do it end to end please", then "Continue"). The bounded Brook pilot completed with a verified candidate pending human acceptance; broad traversal and HelicOps remain stopped. See campaign-harness-rollout.md for measured results and remaining acceptance gates.

Prepared 2026-10-10 UTC from the performance investigation, primary diagnosis,
secondary audit and LoopForge reuse audit. This document consolidates their
recommendations and supersedes their separate suggested implementation orders.
The source audits remain evidence and retain their uncertainty qualifications.

## Intended result

Build a dependable local, sequential process that investigates concrete bugs and
documentation drift, preserves useful evidence between attempts, makes narrow
authorized repairs, and exposes verifiable progress through the automation hub.
Measure successful investigations and accepted fixes, rather than reports,
iterations or cursor movement. Partial investigations remain useful preserved
outputs, but do not become clean-review or closure claims.

Brook is the first repository when restarting is explicitly authorized. HelicOps
remains parked. Its eventual sequence remains Stage 1 hardening/debugging/doc drift
for work needed regardless of the port, then Stage 2 LAUNCH.md implementation on a
dedicated branch. Stage 2 requires a separate operator-controlled transition.

## Constraints and current baseline

- Both campaigns stay parked and the monitoring timer stays disabled throughout
  offline implementation and qualification. No inference, dispatch or live trials
  until explicitly authorized. Re-enabling monitoring is also an explicit rollout
  action, not a side effect of service startup.
- One repository and one model agent at a time, including report finalization,
  independent review and evaluation. Different perspectives run sequentially.
- Retain `qwen3.6:35b-a3b` as the selected model; no Hermes or silent model change.
  Pin the actual digest and effective generation settings for any authorized trial.
- Preserve existing evidence, patches, events and campaign checkpoints. Original
  exhausted/rejected coding tasks remain closed. No reset, replacement contract
  or new issue identity used to evade their caps.
- Keep code-owned authority, strict schemas, idempotency, sandbox requirements,
  fixed checks, immutable in-run limits, terminal-state rules and human acceptance.
  Domain/application dependencies continue to follow AGENTS.md.
- No automatic merge, push, deployment, feature expansion or launch work for Brook.

At the latest audit, Brook was parked at 5/210 and HelicOps at 6/189, with no worker,
occupied slot or n8n running/waiting execution. Thirty-three retained runs whose
objectives mention Brook recorded 14 runtime successes, nine failures, seven
cancellations and three budget exhaustions. These are not unique-task completion
counts. Recorded input/output tokens were 4,895,351/81,364; rejected generations
can be missing, and input includes repeated prefixes. No repository fix was accepted.

Earlier controlled samples showed the selected A3B model was materially faster
than dense 27B in the sampled workloads. They did not qualify autonomous review
quality or establish an SLA. Recent report failures completed in approximately
83–97 seconds, followed by operator checkpoint waits of roughly 10–17 minutes.
Model generation, repeated formatting work and scheduled waiting are separate
contributors and must be measured separately.

## Architecture and ownership

| Component | Responsibility |
| --- | --- |
| Local n8n | Durable outer dispatch, waits and checkpoint propagation; reconnect to existing work without duplicate execution. |
| Campaign controller | Select one issue/experiment, enforce its contract, link its stages and runs, preserve handoffs and derive issue progress. |
| LoopForge runtime | Authoritative action events/reducer, tool policy, budgets, checks, approval gates, retries and execution safety. |
| Provider adapters | Native wire conformance, usage/timing extraction and generation requests, without determining runtime authority. |
| Automation hub | Run controls, readable phase/failure/coverage status and preserved outputs, derived from authoritative records. |

Reuse existing imports and ports. Do not fork a second runtime or add an
independently owning session manager. Keep the existing global slot, service lock,
atomic finish/release, naturally idempotent acceptance and untracked-file diffs.

The intended work sequence is:

`discovery → focused evidence → reproduction/claim check → candidate repair → fixed checks → fresh review → operator acceptance → closure`

A hypothesis can instead be rejected, deferred, blocked or preserved as partial.
Model agreement and literal quotation validity do not establish a reproduced bug.

## Phase 0 — Freeze evidence and define qualification before tuning

Deliverables:

- Version a local fixture suite using the secondary audit's offline reproductions
  and representative historical failures. Keep training/tuning examples separate
  from held-out cases.
- Record the installed provider version, model digest, prompt/tool/schema versions,
  immutable settings, repository revision and source snapshot identity.
- Define a baseline result format covering outcome, coverage, failure category,
  stage attempts, known usage, latency and operator/scheduler waits.
- Maintain a finding-to-test-to-change map so each audit finding has an explicit
  disposition. Record previously fixed items as regression requirements.

Qualification cases must cover nonadjacent quotation forgery, zero-read clean
reports, header-only patch reviews, deletion/rename/new file and binary/oversized
changes, insufficient review budgets, secret filenames, malformed billed responses,
lost repeated feedback, unavailable versus failed checks, crashes after side
effects, cancellation, unchanged candidates after review failure, real doc drift
and correct/historical/planned-document negative cases.

Gate: fixtures reproduce the current defects or explicitly document static-only
findings. Golden outcomes and denial cases are fixed before prompt/profile changes.
No live baseline runs are required for this phase.

## Phase 1 — Repair provider conformance, diagnostics and accounting

Changes:

- Correct native Ollama tool-result fields and call/result association. Test actual
  outgoing requests against the installed protocol, including missing call IDs,
  repeated equal error messages, invalid tool-call counts and completion reasons.
  Preserve assistant content/thinking only as required by the selected mode's
  protocol; never elevate it into runtime authority.
- Keep the existing sequence-based observation identity fix and verify it end to
  end. Equal failures from different executions must each reach the model.
- Account for provider-reported consumption before action parsing can discard it.
  Record attempted calls and invalid-output reasons separately from valid actions.
  Unknown transport-failure usage stays explicitly unknown. Debit known usage
  exactly once even when validation fails; all phases share bounded task totals.
- Preserve bounded stdout/stderr, check identity, exit status and failure signature.
  Extend the closed artifact/event vocabulary deliberately where needed. Apply
  code-owned sensitivity handling to durable evidence as well as telemetry.

Reuse: Runtime event/debit paths, sandbox command results, bounded artifacts,
FailSafeTelemetry and provider-response fixtures.

Gate: no lost repeated feedback, no lost known usage, no double debit, native wire
fixtures pass, and automatic failures expose actionable diagnostics without a
duplicate manual command. These fixes do not yet imply trustworthy review success.

## Phase 2 — Establish trustworthy evidence and feasible review contracts

Changes:

- Create one code-owned snapshot manifest policy for ordinary review, candidate
  review and diff evidence. Bind evidence to revision/candidate identity, path,
  before/after side, content identity and observed source intervals.
- Filter `.env*`, key/certificate files and other explicitly excluded content
  consistently across file bodies, search, patches, handoffs and artifacts.
  Enforce confinement, binary/NUL checks, byte/file ceilings and explicit scope.
  Do not rely on telemetry redaction to protect source snapshots.
- Represent deleted files through baseline content plus deletion hunks; classify
  unavailable, binary and oversized inputs explicitly. No silent omission of
  mandatory evidence. Handle renames and added files with explicit identities.
- Validate literal quotations against actual contiguous observed source. Never
  concatenate disjoint excerpts into a synthetic quotation target. Retain exact
  excerpt references and host-materialized quotations.
- Require task-specific evidence obligations. Patch review must inspect actual
  hunks and relevant surrounding source, including before/after evidence where
  necessary. Reading a filename or header is insufficient. Zero-read reports can
  be stored as partial outputs but cannot count as completed investigation.
- Validate review feasibility before editing, using a conservative immutable
  envelope; refine the exact review plan after a candidate exists, within that
  envelope. Reserve capacity for changed hunks, supporting implementation/tests
  and finalization. Oversized work stops for explicit decomposition rather than
  increasing a live budget or waiving required evidence.

Reuse: CheckOutcome composition, RepairCheck hooks, existing source-reference
tools, patch constraints and snapshot/identity primitives. New interval, manifest
and feasibility implementations are required.

Gate: fabricated/disjoint quotations and header-only coverage fail; deleted-file
reviews can complete honestly; confidential files stay excluded in every evidence
channel; infeasible tasks fail before an edit attempt. Valid bounded cases pass.
Coverage checks establish observed evidence, not semantic correctness.

## Phase 3 — Simplify reporting and make acceptance explicit

Changes:

- Separate evidence gathering from report finalization. Persist a compact evidence
  packet and draft findings, then finalize sequentially using an explicit strict
  report schema. Remove the model's need to escape a report inside a tool-argument
  JSON string. Prefer one canonical evidence shape for the new protocol.
- Keep string-valued action proposals for ordinary tools unless a separate change
  is justified. Introduce a narrow typed reporting port/adapter if necessary;
  avoid a broad rewrite of all action contracts merely to support reports.
- Reporting is a bounded stage of the same task, using the selected model and the
  same aggregate authority/attempt/token/time contract. Any finalizer response is
  revalidated, and quotations are resolved from recorded evidence only.
- Compose independent outcomes for schema, source validity, coverage, candidate
  identity, protected-file integrity and fixed checks. Align runtime readiness
  with the exact downstream acceptance conditions.
- Distinguish submitted report, partial investigation, completed bounded claim
  check, verified candidate and operator-accepted repair in persisted state/UI.
- Reject contradictory passed/inconclusive or passed/not-executed outcomes at the
  appropriate boundary. A check that never ran cannot produce success.

Reuse: strict schemas, composed verification, RepairVerifier hooks and independent
graders. Schema-constrained output improves formatting; it supplies no factual
truth guarantee.

Gate: compact frozen cases finalize without surveying source again; unseen
references, malformed payloads and real unresolved blockers fail; invalid finalizer
output cannot manufacture evidence or bypass the original check/acceptance gates.

## Phase 4 — Add a durable issue loop and stage-specific recovery

Changes:

- Add event-backed issue identity, pinned scope/revision, claim, evidence references,
  disposition, reproduction result, next experiment, candidate identity and closure
  obligations. Separate investigation state from terminal run state.
- Retain discovery scopes and review angles as sequential perspectives on concrete
  issues. Replace the fixed 210-unit traversal as the primary progress measure.
  Prioritize actionable evidence and unresolved high-value claims; bound discovery
  so it cannot starve verification of existing issues.
- Preserve rejected hypotheses and their basis, including known active-rights,
  existing-link and intentional-503 counterexamples. Reopen only when changed
  revision/evidence supplies an explicit basis. Do not turn historic findings into
  current defects without checking current source.
- Reuse deterministic follow-up consolidation for stop reason, recent diagnostics,
  prior approaches, remaining stage allowances and next experiment. Keep derived
  source/model text as lower-trust evidence separate from the authorized objective.
- Preflight the required local environment before consuming an editing attempt.
  Distinguish infrastructure, report-format, substantive-review and code failures.
  Future contracts have separate bounded stage counters plus aggregate ceilings.
  A review-format retry reuses only the identical verified candidate; a changed
  candidate invalidates review and check evidence.
- Apply a code-owned repetition rule to automatic checks as well as tool failures.
  Use failure signature and candidate identity; repeated reads alone are not proof
  of a stall. Infrastructure recovery must not silently mutate the run contract.

Reuse: reducers/events, ReliabilityPolicy, inconclusive results, follow-up helpers,
context preservation and issue-linked new runs. Do not reuse an exhausted task by
renaming it or resetting counters.

Gate: restart reconstructs the same issue/next experiment; review-only failures do
not trigger unnecessary edits; inconclusive checks route to environment attention;
repeated unsupported claims stop recurring; all retry paths respect original caps.

## Phase 5 — Unify context, cancellation and n8n recovery

Changes:

- Preserve active objectives, obligations, latest failures and pending approvals;
  supersede stale observations and retrieve exact excerpts on demand. Account for
  the actual outgoing provider history, static text, tool schemas and output reserve.
- Balance stable prefixes/cache reuse with explicit bounded history. A smaller
  selected ModelContext must not leave an unaccounted append-only provider history.
  Record deliberate rebuild/compaction events and dropped evidence references.
  Preserve trust; do not summarize source text into policy or silently truncate.
- Adapt SessionManager's quiesce/lock/control pattern to the existing campaign
  owner. Resolve Runtime.step's repeated-healing/double-verification behavior before
  using it to drive a controller. Prefer one well-tested driver path.
- Add distinct pause-after-unit, cancel-requested and stopped/acknowledged controls.
  Keep the slot until the worker actually exits. Bound the wait for synchronous
  provider calls; only promise interruption if the adapter proves it.
- Test n8n reconnect/dispatch idempotency, HTTP errors, child/parent failures and
  running/waiting executions. A failed n8n execution must not strand an invisible
  live worker or dispatch a duplicate. Stopping n8n alone remains insufficient.

Reuse: context lifecycle, session control concepts, durable resume, fault injection,
service/slot ownership and compare-and-append maintenance. Emergency force-stop is
not a mechanism for killing a live worker or overwriting corrupt history.

Gate: exactly-once intended verification per drive cycle; no repeated side effects
after recovery; honest cancellation-pending status; no early slot release; one repo
and one agent through n8n reconnect and service restart. Full outgoing context stays
within the configured envelope or fails before inference.

## Phase 6 — Expose meaningful progress and maintain documentation

Changes:

- In the Hub show active issue and stage, last meaningful action, elapsed model/tool/
  check time, checkpoint wait, known/unknown usage, context pressure, evidence
  coverage, failure category, remaining allowances and next experiment.
- Distinguish daemon heartbeat, n8n waiting, an active model request, a completed
  unit and an acknowledged operator check. Never describe loaded Ollama as working.
- Preserve reports, diagnostic output, source references, candidate diffs, check
  results, independent review and acceptance/rejection evidence.
- Add derived outcome metrics: reproduced/rejected claims, completed bounded
  investigations, partials, inconclusive checks, verified candidates, accepted
  repairs, false successes and human interventions. Retain historical cursors as
  historical projections rather than retroactively relabeling them as closures.
- Update architecture, API/schema examples, n8n definitions, Hub help and operator
  documentation in each change that alters behavior. Include a migration guide
  for future contracts and preserved legacy records.

Reuse: trajectory metrics, causal provenance, fail-safe telemetry and versioned Hub
reports. Observability failures must not alter authoritative execution state.

Gate: an operator can identify what is happening, why it stopped, what evidence
exists and the permitted next action without parsing raw logs. Documentation and
workflow examples agree with the final implementation.

## Phase 7 — Qualification, measured tuning and controlled rollout

Offline gate:

- Convert audit probes to meaningful regressions with allowed and denied cases.
  Run targeted tests during each phase, then the full relevant suite, strict typing,
  linting, dependency/architecture rules and diff checks before qualification.
- Use existing benchmark runners, ground-truth graders, fault injection and replay.
  Locked tasks must independently reject false success, not merely observe a
  passing primary verifier. Compare a minimal reference loop with the repaired
  harness on the same fixtures to isolate harness overhead.
- Replay is evidence about recorded control decisions, not a prediction of a new
  model's response. Existing passing tests (92 in the secondary audit and 357 in
  the reuse audit, with possible overlap) do not certify the new integration.

Authorized local-model qualification:

- After explicit permission for model trials, run representative and held-out
  tasks serially with `qwen3.6:35b-a3b`; keep repository campaigns parked.
- Pin a baseline profile. Compare sampling, thinking, context and MTP settings one
  factor at a time. Record exact effective parameters/digest, output completion
  reason, cold load, prefill, decode, full request latency, finalization/check time,
  retries and scheduler/operator waits. Use cache counters only if actually exposed.
- Proposed qualification target: at least 90% correct end-to-end outcomes over a
  minimum of 30 locked serial trials, including repeated and held-out cases; zero
  accepted fabricated/unseen citations, secret disclosures, authority expansions,
  weakened protected checks or false closures. Publish denominators and per-case
  outcomes. Any safety/correctness violation fails the gate regardless of percentage.
  This is an engineering target, not a reliability guarantee or an existing policy.
- Measure latency distributions and useful outcomes per elapsed hour. Select a
  profile only on demonstrated quality/throughput, not decode speed alone. Define
  workload-specific latency targets from the measured reference; do not invent a
  universal seconds-per-task SLA or treat cumulative input as uncached GPU work.

Brook pilot:

- Obtain explicit restart authorization after presenting qualification evidence.
  Begin with three to five narrow, distinct issues under bounded contracts; do not
  immediately relaunch the broad campaign. No automatic recycling of old rejected
  objectives. Investigate/doc-drift work comes before any newly authorized repair.
- Perform sequential fresh review and required human acceptance. Stop at the pilot
  checkpoint for outcome review; expand only after explicit authorization.
- Re-enable the 15-minute monitor only as an explicit rollout action, with honest
  limits on scheduling/daemon/network availability. Monitoring supplements immediate
  n8n state transitions; it must not supply the normal recovery clock for formatting
  failures. A heartbeat alone is never evidence of useful progress.
- On a critical gate failure, park through domain events, quiesce the owner, retain
  the slot until exit, preserve artifacts and return to the failing fixture. Roll
  back a future profile version or isolated implementation change without rewriting
  authoritative history. Do not resume or increase limits automatically.

Later optimization: use versioned ExecutionPolicy and evidence-only shadow advice
for routing/context/cadence inside the proven envelope. Keep worker count one and
promote only with explicit evidence and operator action. HelicOps resumes only after
a separately authorized repository switch; its launch branch remains a later stage.

## Finding coverage and completion checklist

| Finding or retained lesson | Planned disposition |
| --- | --- |
| Native `name`/`tool_name` mismatch; lost recurring feedback | Phase 1 wire tests and observation regression. Renderer-specific causal impact remains unproven. |
| Invalid generations omitted from usage | Phase 1 durable attempt/accounting changes. |
| Automatic stdout/stderr lost; automatic failures bypass tool circuits | Phase 1 diagnostics, Phase 4 check repetition policy. |
| Nonadjacent quote forgery; zero-read/header-only success | Phase 2 interval obligations, Phase 3 composed gates. |
| Deleted/oversized evidence missing; inconsistent secret/scope/binary filters | Phase 2 common before/after manifest and explicit omissions. |
| Ten reads incompatible with changed-file limits | Phase 2 immutable feasibility envelope and bounded decomposition. |
| Nested JSON-string schema burden; formatting destroys investigation effort | Phase 3 focused typed finalization and bounded stage recovery. |
| Runtime success differs from final acceptance | Phase 3 aligned verdicts; preserve previously added acceptance guard. |
| Repeated broad surveys, weak rejected-finding memory and fixed matrix | Phase 4 event-backed issue queue and evidence handoffs. |
| Infrastructure/review errors burn coding attempts | Phase 4 preflight and future-contract stage counters; old caps stay closed. |
| Append-only history disagrees with selected context budget | Phase 5 actual outgoing context accounting. |
| Slow stop; step repeats verification; n8n/worker lifecycle mismatch | Phase 5 owned cancellation, cycle semantics and reconnect tests. |
| Cursor/tokens/heartbeat confused with useful work | Phase 6 outcome and stage visibility. |
| Untested profile, cache effects, operator gaps and lack of end-to-end qualification | Phase 7 controlled comparisons, held-out trials and bounded pilot. |
| Commit acceptance idempotency, new-file diffs, global ownership and sandbox contracts already work | Preserve and regression-test; do not rebuild or weaken. |

Implementation is complete when all rows have verified dispositions, documentation
matches behavior, offline gates pass and a reviewable qualification report exists.
Operational readiness additionally requires the authorized local-model gate and
Brook pilot. Neither code completion nor a single successful smoke run authorizes
an unattended long campaign.

## Evidence and research

Local evidence under `.loopforge/campaign/monitor/`: `performance/diagnosis-and-fix.md`,
`campaign-system-diagnosis.md`, `campaign-system-diagnosis-data.json`,
`secondary-audit/report.md`, `secondary-audit/reproduce.py`,
`secondary-audit/reproduction-results.json`, and `loopforge-reuse-audit.md`.
These local artifacts preserve observations and reproduction limits; this plan
does not upgrade static findings or one-sample benchmarks into measured guarantees.

The earlier research supports simple composable flows, actionable tools, deliberate
context selection and outcome-based evaluation. See [agent design](https://www.anthropic.com/engineering/building-effective-agents),
[tool design](https://www.anthropic.com/engineering/writing-tools-for-agents),
[context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
and [agent evaluations](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).
Provider implementation follows [Ollama tool calling](https://docs.ollama.com/capabilities/tool-calling)
and [structured outputs](https://docs.ollama.com/capabilities/structured-outputs).
Profile experiments should reference the selected model's
[official model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B), while measuring the actual local digest/settings.
