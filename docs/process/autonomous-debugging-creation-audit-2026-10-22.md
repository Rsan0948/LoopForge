# Autonomous debugging and creation: follow-up audit

Date: 2026-10-22  
Scope: current working tree, after the repair protocol correction  
Disposition: diagnosis and implementation recommendations; no application changes

## Executive summary

LoopForge has useful foundations: durable events, strict model boundaries, isolated
candidates, independently measured acceptance, bounded spending, and explicit review.
However, its repository-goal flow still behaves like a controller for one prepared
repair assignment. It does not yet provide the flexible technical management needed
for general overnight debugging and feature creation.

A stronger supervisor can reason better, but cannot overcome missing access to the
current candidate, unreachable evidence, an immutable mistaken assignment, or a
workflow that requires a failing baseline before starting a feature.

The most urgent work is to make recoverable mistakes recoverable, make evidence
complete and accessible, and distinguish debugging from creation. Then add durable
milestones and frontier review. Changing model serving engines does not address
these control-flow limitations.

The intended division remains a frontier supervisor/reviewer with a local coding
worker. This report does not propose automatically replacing the coding worker.

## Method and limits

- Inspected the goal reducer, coordinator, Gemini adapter, repository investigation,
  worker composition, coding/review gates, evidence stores, clarification/review
  controls, console routes, and relevant tests.
- Rechecked the previous six findings and traced additional failure paths.
- Ran six targeted unit-test modules: **100 passed in 9.51 seconds**.
- Ran three additional offline probes using disposable temporary journals and
  fixture repositories. Results are recorded below.
- No paid Gemini calls, local model inference, production goal execution, or
  benchmark changes were performed in this audit.
- Findings describe the current dirty working tree, not just committed HEAD.
- “Confirmed” means a code path or constraint was established. It does not mean
  every issue occurred in the historical recurring-repair trial. That trial has a
  separate [diagnosis](recurring-repair-loop-diagnosis.md).

The earlier [repair correction](repair-protocol-correction.md) remains valuable.
Its live probe demonstrated one useful edit, not unattended completion of the app.

## Findings

### A01 — One incorrect supervisor action can terminate the whole goal

**Priority: P0. Confirmed and reproduced. Newly identified.**

`GoalCoordinator._execute` turns an action outside the current allowed set into a
terminal `blocked` result. `_investigate` similarly turns any caught `ValueError`
into a terminal policy denial. A nonexistent inspected filename, oversized source
artifact, or invalid evidence ID can therefore end the goal instead of producing
feedback the model can correct. The investigation error's specific cause is also
replaced by a generic summary.

The provider schema advertises the full action enum; the narrower allowed set is
provided in context. A schema-valid action can still be invalid for the phase.

**Probe:** proposed `verify` with an absent worker. After one decision, the goal
became `blocked`: “Supervisor action denied by current worker state.” No correction
opportunity remained.

**Recommendation:** add typed, durable rejected-action observations with safe,
specific feedback. Allow bounded correction when no side effect occurred. Separate
invalid proposals, missing artifacts, policy violations, and adapter failures.
Keep the actual authority checks; do not execute denied actions.

**Acceptance:** an incorrect phase action and a mistyped source path can each be
corrected; repeated invalid proposals exhaust a bounded allowance; out-of-scope
access never executes.

Sources: `src/loopforge/application/supervisor.py:244`, `:295`;
`src/loopforge/adapters/gemini_supervisor.py:74`.

### A02 — Supervisor source inspection remains pinned to the original checkout

**Priority: P0. Confirmed. Revalidated.**

`RepositoryInvestigation._inspect` reads `config.campaign.revision`. Research
records are also required to match the original checkpoint. Worker observations
carry patch/report summaries, but subsequent supervisor source inspection cannot
directly inspect the changed candidate through this interface.

**Recommendation:** expose separate baseline and candidate inspection operations.
Bind candidate evidence to its exact identity. Preserve the original authorization
checkpoint while allowing observations of authorized derived candidates.

**Acceptance:** after an edit, the supervisor can read the new source and compare it
with the baseline; a stale candidate reference is explicitly rejected or labeled.

Sources: `src/loopforge/entrypoints/goal_investigation.py:217`;
`src/loopforge/domain/goals.py:463`.

### A03 — Omitted worker evidence has no matching supervisor retrieval path

**Priority: P0. Confirmed by interface tracing. Newly identified.**

`CampaignGoalWorker.observe` replaces evidence exceeding 24,000 bytes with an
omission notice saying to “retrieve locally.” It supplies a patch hash, but no
retrievable worker artifact ID. The supervisor's `retrieve` action only accepts
IDs already present in `state.research`, backed by `ResearchStore`.

Thus the advertised recovery instruction cannot retrieve that omitted payload
through the current supervisor tools.

**Recommendation:** put worker reports, diffs, and verification output in the
bounded artifact service. Return artifact IDs and continuation information instead
of dropping the whole evidence packet. Preserve a small failure summary inline.

**Acceptance:** a worker result larger than 24 KB remains fully inspectable in
pages, with integrity checks and scope enforcement.

Sources: `src/loopforge/entrypoints/goal_worker.py:84`;
`src/loopforge/entrypoints/goal_investigation.py:103`;
`src/loopforge/adapters/research_store.py`.

### A04 — Evidence paging cannot recover the tail of a long line

**Priority: P1. Confirmed and reproduced. Newly identified.**

Supervisor inspection/retrieval pages by line. `_record` truncates an excerpt at
8,000 bytes, and the Gemini context further limits recent excerpts to 2,500
characters. There is no column or byte cursor in `InspectionRequest`.

**Probe:** stored one 9,000-character line followed by `TAIL_SENTINEL`. Retrieving
line 1 returned 8,000 characters without the sentinel; line 2 returned an empty
excerpt. The remaining text was unreachable through line paging. JSON logs and
minified source can exhibit this shape.

**Recommendation:** use byte/column-aware continuation that accounts for the
final provider-context limit. The local worker already has column-aware source
reading; the supervisor needs equivalent completeness.

**Acceptance:** concatenate pages of long UTF-8 lines to recover the exact original
artifact, including text beyond both truncation boundaries.

Sources: `src/loopforge/domain/investigation.py:37`;
`src/loopforge/entrypoints/goal_investigation.py:103`, `:177`;
`src/loopforge/adapters/gemini_supervisor.py:198`.

### A05 — Individually bounded context fields can still cause terminal admission failure

**Priority: P1. Confirmed and reproduced. Newly identified.**

The Gemini request has a 48,000-byte serialized limit. Goal, plan, worker evidence,
investigation options, and research memory are assembled before this check.
Nested JSON escaping further increases the final size. Admission failure causes
the coordinator to block immediately rather than build a smaller valid packet.

**Probe:** an 8,000-character goal, 4,000-character plan, and 22,013-byte worker
evidence string produced “Supervisor input exceeds admission or credential
exclusion policy.” Each component was within its relevant individual limit.
The probe used a dummy credential and made no request.

**Recommendation:** allocate the final serialized request budget by priority.
Keep authority, current failure, and candidate identity; replace lower-priority
evidence with retrievable references. Preserve strict credential exclusion.

**Acceptance:** quote-heavy and Unicode-heavy inputs either produce a useful
bounded request with accessible omissions or fail intake before starting work.

Sources: `src/loopforge/adapters/gemini_supervisor.py:158`, `:228`;
`src/loopforge/application/supervisor.py:175`.

### A06 — General creation is gated on a failing behavioral baseline

**Priority: P0. Confirmed. Revalidated.**

Repository goals require acceptance probes. Delegation requires
`baseline_reproduced`, set when baseline execution records a behavioral probe
failure. Although task `kind` supports `feature` and `launch`, this investigation
gate does not distinguish those intents.

Creation can work with prewritten failing feature probes; a healthy baseline alone
cannot satisfy this gate. Preparing those probes is itself work the current intake
expects to have already been done.

**Recommendation:** distinguish bug reproduction, creation baseline measurement,
and exploratory investigation. Establish observable feature criteria without
requiring a preexisting failing application. Keep independent final acceptance.

**Acceptance:** add a feature to a passing repository and create a small application
from an approved scaffold without manufacturing an unrelated failure.

Sources: `src/loopforge/entrypoints/goal_investigation.py:54`, `:136`;
`src/loopforge/domain/goals.py:327`;
`src/loopforge/entrypoints/repository_goal_models.py:40`.

### A07 — Permission to edit a file becomes an obligation to edit it

**Priority: P0. Confirmed. Revalidated.**

Assignment files become both `allowed_paths` and `required_changed_paths`.
`CodingVerifier` rejects candidates missing any required change. An initially
plausible file list can force unnecessary edits after investigation shows that
fewer files need modification.

**Recommendation:** separate allowed scope, suggested locations, and genuinely
required deliverables. Judge ordinary tasks by behavior rather than a guessed
file-change checklist.

**Acceptance:** a correct one-file repair succeeds when two files were permissible;
an explicitly required artifact remains mandatory.

Sources: `src/loopforge/entrypoints/goal_investigation.py:86`;
`src/loopforge/entrypoints/campaign_coding.py:656`.

### A08 — One frozen assignment cannot express an evolving implementation plan

**Priority: P1. Confirmed. Revalidated with an important qualification.**

The goal stores one assignment and rejects replacement. Environment preparation
also closes after assignment. This prevents milestone-based scope refinement and
switching to another already-approved environment after a later discovery.

The latest supervisor plan and rationale **do reach the worker** through
`assignment_guidance`. It would be incorrect to claim retries receive no new
supervisor advice. The limitation is structural assignment/environment evolution,
not total loss of guidance.

**Recommendation:** add durable milestone and assignment versions within the
original outer authority. Reconcile running work before changing an assignment.
Environment changes must come from preauthorized choices and invalidate affected
verification. Never reactivate a terminal run; use linked successors.

**Acceptance:** investigation → backend → UI → end-to-end verification survives a
restart and retains verified progress when a later milestone needs revision.

Sources: `src/loopforge/domain/goals.py:485`;
`src/loopforge/entrypoints/repository_supervisor.py:39`;
`src/loopforge/entrypoints/goal_investigation.py:120`.

### A09 — Clarification is an intake feature, not an adaptive supervisor capability

**Priority: P1. Confirmed. Newly detailed.**

Repository intake permits one clarification topic. Questions must match frozen
topics and cannot be opened after assignment. The supervisor action vocabulary
has no `ask` operation. Optional timeout fallback exists, but for the predeclared
question and original checkpoint, not an ambiguity discovered midway through work.

Late answers conflicting with an experiment stop continuation; the current UI
instructs the user to create a new goal. That preserves authority but leaves the
handoff manual.

**Recommendation:** allow dynamic questions under code-owned categories and frozen
fallback policy. Optional uncertainty can create a linked isolated experiment
from the current verified checkpoint. Permission questions must never time out
into new authority. Add a guided successor flow for conflicting late answers.

**Acceptance:** a mid-task optional ambiguity falls back after its deadline;
permission uncertainty does not; a late answer preserves the experiment and
creates a reviewable continuation without rewriting history.

Sources: `src/loopforge/entrypoints/repository_goal_models.py:44`;
`src/loopforge/domain/goal_review_reducer.py:27`;
`src/loopforge/application/supervisor.py:104`; `ui/src/views/GoalsView.tsx:71`.

### A10 — Broad file protection prevents legitimate feature maintenance

**Priority: P1. Confirmed constraint; impact depends on task. Newly identified.**

Existing files under `tests`, `test`, and `scripts`, plus several manifests and
lockfiles, are protected. Hidden path components are excluded from writable scope.
New regression tests are allowed when within scope, but existing tests and package
configuration cannot be updated even for a legitimate approved feature change.

**Recommendation:** distinguish immutable independent acceptance artifacts from
ordinary repository tests, build configuration, and dependency manifests. Declare
which classes are editable at intake, with appropriate checks and review. Do not
let the worker modify the trusted acceptance oracle to obtain a pass.

**Acceptance:** an approved dependency/test update can complete; changing an
independent acceptance probe, credentials, or unapproved configuration is denied.

Sources: `src/loopforge/entrypoints/campaign_coding.py:129`, `:240`;
`src/loopforge/entrypoints/campaign_models.py:370`.

### A11 — Frontier supervision does not currently mean frontier code review

**Priority: P1. Confirmed routing. Newly identified.**

The supervisor chooses `verify`, but `build_review` normally constructs an
`OllamaModel` using the campaign model. Gemini receives the resulting evidence; it
is not the main source-reading reviewer in that path. This falls short of the
desired frontier strength specifically in review and orchestration.

**Recommendation:** make review-role routing explicit. Give the frontier reviewer
current source/diff/artifact access and a structured issue/acceptance report.
Independent execution still decides whether required checks passed. Keep the
local coding worker as the default implementation role.

**Acceptance:** record which provider performed substantive review, prove it saw
the exact candidate, and show that unsupported claims cannot mark a candidate
verified.

Sources: `src/loopforge/entrypoints/campaign_execution.py:415`;
`src/loopforge/entrypoints/campaign_review.py:1240`.

### A12 — Supervisor and repair-specialist spending use separate budgets

**Priority: P1. Confirmed accounting separation. Newly identified.**

Goal inference uses the goal journal and `max_cost_microusd`. Repair escalation
creates a separate `RepairJournal` using `repair_budget_microusd`, with a ledger
under the worker task. The morning report's remaining balance subtracts only goal
state spending/reservations. Specialist calls report runtime usage but are not
combined into that goal balance by the inspected composition.

This is not evidence that either individual cap is bypassed. It means the displayed
goal budget is not a complete all-provider spending envelope when repair escalation
is enabled.

**Recommendation:** reserve all paid supervisor, review, and specialist calls
against one parent ledger, with role-level allocations. Report known spend and
unknown-outcome reservations separately.

**Acceptance:** concurrent or interrupted role calls cannot exceed the parent cap;
the morning report reconciles to all paid provider receipts.

Sources: `src/loopforge/entrypoints/campaign_coding.py:1238`;
`src/loopforge/entrypoints/campaign_execution.py:348`;
`src/loopforge/adapters/repair_journal.py`;
`src/loopforge/entrypoints/goal_review.py:32`.

### A13 — Recovery still needs semantic failure categories and progress evidence

**Priority: P1. Confirmed classification gap; tuning needs trials. Revalidated.**

`worker_failure` defaults most model-attempt errors to provider failure, with
special cases for only selected coding errors. Repair protocol mistakes can thus
be confused with transport/provider failures. The forced repair mode also retains
a small mechanical investigation allowance; revisable decisions are opt-in.

At the supervisor level, repeated research is detected by kind, content hash, and
excerpt. Different excerpts can reset stagnation without proving task progress.
The default 12 decisions include exploration and provider attempts as well as
execution management. A larger feature can consume them before delivery.

**Recommendation:** distinguish protocol, hypothesis, environment, verification,
context, and authority failures. Require successors to explain what changed.
Track resolved questions, reproduced failures, candidate changes, and verified
milestones. Size frozen budgets from the intended workload, rather than merely
raising retry counts after a failure.

**Acceptance:** repeated unchanged attempts escalate; useful investigation can
continue within its budget; a new hypothesis does not reset hard limits.

Sources: `src/loopforge/application/failure_recovery.py:9`;
`src/loopforge/domain/goals.py:496`;
`src/loopforge/entrypoints/supervisor.py:34`;
[repair correction](repair-protocol-correction.md).

### A14 — The console does not own a durable execution service

**Priority: P1 for overnight product use. Confirmed console/CLI separation.**

The goal API lists/reports existing goals and appends operator commands. The UI
directs users to create goals through the CLI. Execution is a separate CLI loop.
The `resume` command changes journal state; it does not start a missing runner.

Durable journals support recovery once a runner is started again. They are not
themselves a scheduler or process supervisor. External service management may
exist on an operator's machine, but it is not established by these app paths.

**Recommendation:** add service-owned start/recovery, a heartbeat/lease, explicit
queued/running/offline status, and startup reconciliation. Surface missing local
model/container dependencies before accepting an unattended task.

**Acceptance:** kill the runner midway, restart the app/service, and verify safe
automatic reconciliation; distinguish paused work from work with no live runner.

Sources: `src/loopforge/entrypoints/goal_routes.py`;
`src/loopforge/entrypoints/supervisor.py:229`;
`src/loopforge/entrypoints/repository_supervisor.py:258`;
`ui/src/views/GoalsView.tsx:60`.

### A15 — Passing infrastructure tests is not model autonomy qualification

**Priority: P1 release gate. Confirmed test limitation.**

The inspected repository integration fixture uses a mocked Gemini decision
function and scripted worker edits. These tests establish important orchestration,
isolation, and persistence properties. They do not measure whether real models
discover the right files, recover from a bad hypothesis, or complete an unfamiliar
feature. The recent live repair probe covered one decision boundary.

**Recommendation:** retain deterministic tests and add a small repeated live
qualification set with fixed budgets, independent outcome checks, and recorded
prompts/actions. Report completion, useful partial progress, justified blockers,
protocol failures, intervention count, cost, and duration separately.

Sources: `tests/integration/test_repository_supervisor.py:55`, `:125`;
[repair correction](repair-protocol-correction.md).

## Recommended implementation order

1. **Close immediate dead ends:** A01–A05. Typed corrective feedback, current
   candidate inspection, retrievable artifacts, complete paging, and context packing.
2. **Make creation a supported workload:** A06–A08 and A10. Separate task intent,
   edit scope, outcome requirements, milestones, and protected acceptance artifacts.
3. **Make frontier management substantive:** A09, A11–A13. Dynamic clarification,
   actual frontier review, combined spending, and evidence-based recovery.
4. **Qualify overnight operation:** A14–A15. Service lifecycle and repeated
   end-to-end trials, including real creation and wrong-hypothesis recovery.

Each implementation should include allowed behavior and denial/failure coverage.
Do not loosen immutable authority to make a benchmark pass. Do not reopen terminal
runs; create linked successors preserving candidate work and provenance.

## Minimum end-to-end qualification matrix

| Scenario | Required result |
| --- | --- |
| Bug, wrong first hypothesis | Inspect current evidence, revise, repair, independently verify |
| Feature on passing baseline | Establish baseline, implement feature, verify criteria |
| Multi-stage feature | Complete dependent milestones without repeated human prompting |
| Oversized report / long source line | Retrieve all needed evidence without losing the goal |
| One invalid supervisor proposal | Return actionable denial, then accept a corrected proposal |
| Repeated invalid proposals | Stop boundedly with retained work and a specific reason |
| Approved test/dependency change | Edit permitted project files while protecting trusted checks |
| Mid-task optional question | Wait, branch under approved fallback, report assumptions |
| Permission question | No timeout-based authority expansion |
| Runner termination | Reconcile persisted intent without duplicated effects |
| Frontier review finds a real issue | Return precise feedback and verify the corrected candidate |
| Shared paid budget exhausted | No additional paid call; accurate aggregate report |

Use multiple repetitions and more than one repository shape. Define the success
threshold before running the evaluation. A deterministic task failure and a
justified external blocker must not be presented as the same outcome.

## Reproducible validation performed

```sh
.venv/bin/python -m pytest -q \
  tests/unit/test_goal_coordinator.py \
  tests/unit/test_repository_investigation.py \
  tests/unit/test_goal_review.py \
  tests/unit/test_gemini_supervisor.py \
  tests/unit/test_assignment_guidance.py \
  tests/unit/test_worker_failure_recovery.py
```

Result: **100 passed in 9.51s**. No network inference was used.

Additional probes used the existing unit-test fixture constructors in temporary
directories and called production code directly:

| Probe | Input / operation | Observed result |
| --- | --- | --- |
| Wrong phase | Absent worker; supervisor proposes `verify`; two coordinator steps | Terminal `blocked` after one decision |
| Long line | Research artifact: 9,000 `x` characters + `TAIL_SENTINEL` + newline; retrieve line 1 then line 2 | 8,000-character prefix, then empty page; sentinel absent |
| Context admission | 8,000-char goal; 4,000-char plan; JSON patch containing 11,000 quote characters; dummy key; call `prepare` only | Admission failure despite 22,013-byte worker evidence being below 24 KB |

These probes establish specific edge cases. They are not completion benchmarks,
and this report does not claim that model quality is irrelevant. The current
interfaces impose limits that should be corrected before interpreting further
stalls as evidence against the model.
