# Autonomous supervisor implementation progress

Date: 2026-10-20 (America/Denver)
Branch: `feat/autonomous-supervisor-foundations`
Source plan: [autonomous-supervisor-plan.md](autonomous-supervisor-plan.md)

## Baseline and authorization

Preserved 330 tracked/untracked source and documentation files, their SHA-256
hashes and modes, the original HEAD, status and binary patch in the ignored
`.loopforge/supervisor-baseline/20261021T003214Z/` directory. The archive excludes
ignored secrets and local runtime state. Each copied SQLite database passed its
integrity check. These are individually consistent database snapshots, not a
coordinated full-service restore backup; candidate checkouts remain untouched.

The loaded campaign service matched the source fingerprint before these edits:
`8964ec91390f5c67750c35fbe49a7bb93bbfc509d8b546abaffbb571a84c186c`.
Brook was idle at checkpoint cursor 210; HelicOps was parked at cursor 6.
Neither had a worker or reserved slot. The upload candidate
`coding_7c443c3f7ba5` remained reviewed/patch-ready. No pending action was
accepted, retried, replayed or dispatched. The running service has not been
restarted into the changed code.

The captured installed model identity was `qwen3.6:35b-a3b`; this is installation
metadata, not a live inference or residency test. Local Python is 3.14.7; type
checking targets the declared Python 3.12 floor. Node 22.23.3 is available and
used for UI checks because the default Node installation has a missing dylib.

The user authorized **$5 total Gemini evaluation spend**. Current conservatively
accounted spend is **$0.114208** after the Phase 3 synthetic qualification.
Repository material sharing, automatic local acceptance, clarification deadlines,
and overnight rollout limits remain unconfigured. An ignored local policy record
retains the authorization. Phase 2 now enforces goal and shared-ledger
reservations. Only generated fixture source/evidence was sent to Gemini; no
production repository material was shared.

## Frozen evaluation input

[autonomous-supervisor-evaluation-lock.json](autonomous-supervisor-evaluation-lock.json)
records the existing 12-task benchmark suite, its spec/content hashes, objectives,
independent graders and planned three repeats. It also lists the additional
supervisor control scenarios. Phase 2 adds one clean live synthetic evaluation,
recorded below; the frozen representative suite is still a later qualification
gate. Existing benchmark coverage alone does not establish overnight reliability.

## Implemented foundation slice

- Reproduced same-file discovered-evidence loss with a failing regression.
- Retained observations outside mandatory intervals, preserving original excerpt
  identities and ranges. Required and optional spans can be compacted separately.
- Report packets explicitly identify omitted spans by excerpt, path and range;
  original observed text remains available through the evidence tools. Mandatory
  coverage cannot be discarded to fit a packet.
- Added code-owned passing-checkpoint versus task-completion verdicts, durable
  replay and index support, counterfactual preservation and operator visibility.
- Added restart, incomplete acceptance, repeated-checkpoint, exhaustion and strict
  boundary regressions. Historical verification events retain their semantics.

The completion change is a runtime foundation. Legacy campaign verifiers still
interpret their configured checks as whole-task verification. A supervisor must
supply an independent acceptance-criteria verifier before arbitrary multi-part
campaign goals can rely on the new checkpoint distinction.

## Remaining gates

Baseline restore qualification and production remote-data configuration remain
outstanding. Evaluation authority is bounded to generated synthetic material.
Phases 1–2 are qualified for prepared Python probes and one prepared local worker.
Phase 3 is now qualified for a bounded multi-file repository goal with source
and task discovery, approved environment preparation and recovery, durable
memory, restart and independent verification. Legacy checks-only tasks retain
their previous semantics. Phase 4 clarification, experiment, dirty-checkout and
review UI work, followed by broader qualification and rollout, remains as
specified in the source plan.

## Validation

- Original campaign baseline: 287 tests passed before implementation.
- Full non-live suite after implementation: 2,814 passed, 63 skipped; 92% coverage.
  Skips reflect unavailable Postgres, platform isolation capabilities and the
  existing filesystem capability gate; they are not successful execution claims.
- The final checkpoint module has 19 passing tests, including an additional
  regression added after the full run: interruption immediately after a checkpoint
  event, before the next plan, preserves both the SQLite index and replay state.
- Ruff format/check, strict Pyright and both import-layer contracts passed.
- Console typecheck and production build passed with Node 22.23.3.
- One pre-existing blank-line formatting issue in a campaign coding test was
  corrected to satisfy the repository-wide format gate.

A focused patch against the preserved dirty checkout is available locally at
`.loopforge/supervisor-foundation-review.patch`; it distinguishes this work from
pre-existing changes. No changes were committed, pushed or deployed.


## Hardening follow-up

Reviewed the foundation against its preserved baseline and reproduced failures
before applying fixes. The review found:

- Required blank-line spans were shortened or represented as empty files after
  splitting observations. Coverage checks also lost trailing blank lines in
  actual tool reads. Report assembly now preserves literal LF-delimited spans;
  coverage uses pinned source metadata to distinguish blank lines from empty
  files. Empty-file claims cannot satisfy nonempty source obligations.
- Compaction reconstructed source from rendered strings, which lost blank lines
  and could reject packets unnecessarily. It now protects required structured
  line ranges and empty-file metadata directly, retaining explicit omission
  records for optional spans.
- The success grader could reuse an earlier whole-task pass after a later
  checkpoint or failed verification. It now requires the latest verification to
  confirm completion for the same run before its terminal stop. Foreign-run and
  future receipts are denied.
- Evaluation hook evidence included intermediate checkpoints and superseded
  passes. Only the latest whole-task passing verification is admitted as final
  acceptance evidence; all earlier summaries remain available for diagnostics.
- Counterfactual replay and the fixed-roster orchestrator lost the inconclusive
  flag on failed checks. Both preserve it. Session detail now derives that flag
  from the latest verification, clearing stale unavailability after a later pass.
- Verifier boundary validation accepted boolean progress scores and non-boolean
  inconclusive flags. These now fail validation before runtime recording.

Added regression tests for these cases, allowed/denied empty-file coverage,
strict boundary values, and a property test with 40 generated examples checking
exact preservation of blank lines, control characters and Unicode separators.

Final verification: **2,850 passed, 63 skipped; 92% coverage**. Ruff format/check,
strict Pyright, Import Linter and whitespace checks passed. Console source and
wire fields were unchanged in this follow-up; backend tests exercise both
completion and checkpoint API responses. No live campaigns or paid calls ran.
At the end of that hardening pass, the remaining Phase 1 gates were still open.


## Phase 1 qualification completed

Closed the remaining Phase 1 work before advancing to the durable coordinator:

- Complete-request context admission now records byte size, measurement basis,
  input token bound, output/framing reservations and compaction count. Successes
  and failures persist diagnostics in run events and the console exposes them.
  The byte estimate remains explicitly conservative, with a code-owned token
  counter seam. Output reservation is enforced as an actual generation limit.
- Compaction preserves current authorized-human and runtime-policy items by
  trust class, including constraints with unfamiliar item names. Provider
  measurements are not fabricated during scripted counterfactual replay.
- Operator-frozen `acceptance_probes` run outside editable project-test files,
  inside the existing immutable-image, read-only-workspace container contract.
  They require a successful exit and the trusted probe's completion marker;
  an early zero exit does not certify the result. Probes never reuse receipts.
- Typed acceptance receipts bind criterion/check results to the base revision,
  candidate bytes/modes, versioned frozen task contract and verification run.
  Missing evidence, contradictory typed outcomes, contract mutation, edits during
  checks and stale receipts are denied. Coding and review receipts persist in
  the append-only coding ledger; local acceptance requires fresh successful
  review evidence matching the current candidate.

The real container regression uses an editable project test that always passes.
It rejects both incorrect arithmetic and an imported program that exits zero
before the acceptance assertions. The correct patch then passes; composition
restarts from the ledger, fresh review creates a different run-bound receipt,
and a later edit prevents acceptance. Only the isolated fixture candidate is
committed; its original source checkout remains unchanged. Model decisions in
this test are scripted, so it is a deterministic harness qualification.

Final gates: **2,888 passed, 63 skipped, 92% coverage**, Ruff format/check,
strict Pyright, architecture tests, Import Linter, UI typecheck and production
build. No paid model call, live campaign dispatch, service restart, push or
deployment occurred. At this Phase 1 gate, Gemini spend was **$0 of $5**.

See [ADR-0017](../decisions/ADR-0017-trusted-acceptance-and-context-admission.md)
for the contract and limits. New independent probes support Python and must be
explicitly configured; existing persisted tasks are not silently converted.
Phase 2 was not implemented by the Phase 1 qualification; its completed work
and separate evidence follow.


## Phase 2 — durable prepared-goal qualification

Implemented a domain event/reducer goal journal, provider-independent supervisor
and worker ports, strict Gemini adapter, and application coordinator. Prepared
intake freezes the full task/worker/policy binding. Decisions cannot change
commands, paths, capabilities, hard limits or approval requirements. Delivery
rechecks the exact candidate and independent acceptance evidence.

Atomic inference reservations count settled spend and concurrent reservations
against immutable goal/shared-ledger caps. Usage includes billed thinking;
missing usage and interrupted calls consume their full reservation. Backoff,
recovery and decision counts persist. Worker attempts retain aggregate task
limits and the goal deadline narrows new runtimes. Durable stage-specific keys
reconcile enqueue/retry/dispatch after lost acknowledgement without duplicate
work or hidden paid retries. No external chat acknowledgement is involved.

The opt-in CLI supports create/run/status/cancel in a separate goal directory.
It connects the existing local coding and fresh review engine without invoking
generic campaign lanes. Delivery leaves a verified `patch_ready` candidate and
never grants local acceptance, remote push, merge or deployment. Existing
production campaigns, pending upload work and the running service were untouched.

Qualification evidence:

- Network-free controls and real-container fixture tests cover lost inference,
  budgets, strict output, authority denial, stale candidates, terminal states,
  immutable intake, service ownership and restart after lost worker acknowledgement.
- A live generated arithmetic task exposed relative checkout cloning and missing
  campaign resumption during retry. The test retained these failures and required
  implementation repair. A failure-injection regression also caught a coding
  retry replay incorrectly dispatching review; operation intent now records its
  original stage and replay cannot advance it.
- A separate clean live task completed with `gemini-4-argon` at medium thinking
  and local `qwen3.6:35b-a3b`, with an automatic service restart between coding and
  fresh review, independent Python acceptance probes, and candidate delivery.
  No chat intervention or implementation repair occurred during the clean run.
  Its shallow editable project test was not accepted as the sole grader.
- Original generated source remained unchanged; both candidates stayed unaccepted.
- Durable artifacts live in `.loopforge/qualification/phase2-live` (repaired run)
  and `.loopforge/qualification/phase2-live-clean` (clean run). Each has a fixed
  $0.50 ceiling, for at most $1 preallocated inside the user's $5 authorization.
  Charges were $0.011454 and $0.009554 respectively: **$0.021008 total** from
  reported usage with conservative cache accounting, not a verified account bill.

See [ADR-0018](../decisions/ADR-0018-durable-prepared-goal-supervisor.md) and
[the operator guide](../architecture/prepared-goal-supervisor.md). Prepared
contracts and Python probes remain prerequisites. Generalized investigation,
repository retrieval, environment/task preparation and ordinary-failure recovery
are Phase 3; one clean synthetic case is not an overnight reliability claim.

Final Phase 2 gates: **2,938 passed, 63 skipped, 92% coverage**, with no
resource warnings. Ruff format/check, strict Pyright, architecture contracts and
`git diff --check` pass. The 50 focused Phase 2 tests also passed with resource
warnings treated as errors. Full output and consistent SQLite qualification
snapshots are preserved under `.loopforge/qualification/phase2/`; the focused
review patch against the completed Phase 1 state is
`.loopforge/supervisor-phase2-review.patch`. No production service restart,
production task dispatch, production source sharing, acceptance, push or deployment occurred.


## Phase 3 — flexible investigation and task preparation

Added scoped source/directory inspection from an explicit pinned revision,
original-evidence retrieval by identity and line range, approved environment
selection and readiness measurement, and independent behavioral reproduction.
The supervisor chooses an approach without mandatory campaign lane traversal.

Repository intake freezes a broad editable scope and behavioral acceptance,
without exact changed-file deliverables. The supervisor proposes the narrower
assignment after investigation. Code validates source dependencies, scope,
checkpoint and environment, then resolves all commands, probes, image and limits
from that immutable authorization. The original user objective remains unchanged;
assignment and recovery guidance enters worker context as `MODEL_INFERENCE` with
its own provenance rather than authorized-human/runtime-policy content.

Research records have stable operation keys, revision/content identities,
explicit omissions and original content in an immutable evidence store. Their
events/reducer maintain durable memory, environment readiness, reproduction and
assignment state. Repeated evidence cannot count as progress merely because a
query changes. Research failure and meaningful-progress limits bound recovery.
Typed worker/check/stop evidence distinguishes provider, context, no-progress,
verification, environment, policy and unknown failures.

Qualification:

- The first live generated trial stopped at a directory-inventory mismatch.
  Directory inspection was fixed and the blocked terminal goal retained. It
  consumed $0.004400; this implementation intervention is not hidden.
- The separate clean trial inspected the source, recovered from an injected
  transient container failure and missing pytest, selected the approved stdlib
  environment, and reproduced both independent behavioral failures. It restarted
  with investigation memory intact, selected the two implementation files,
  recovered from an invalid local worker response using a bounded successor,
  completed fresh review and fixed checks, and delivered a candidate.
- No chat intervention or harness repair was needed during that clean trial.
  The original source remained unchanged and the candidate stayed unaccepted.
  Clean-trial usage-based cost: $0.088800. Total across Phases 2–3:
  **$0.114208 of the authorized $5**, conservatively estimated from usage rather
  than an independently verified account bill.
- Each Phase 3 trial has a fixed $0.75 ceiling. Combined with the two Phase 2
  $0.50 ceilings, preallocated maximum authority is $2.50 within the $5 budget.
  Reruns cannot reset goal deadlines, spending or terminal state.
- Network-free control tests and real-container integration cover original
  evidence, scope denial, immutable receipts, environment/reproduction gates,
  zero-exit/unexecuted checks, source-dependent narrowed assignments, guidance
  trust, meaningful progress, failure categories and restart.
- Final non-live suite: **2,967 passed, 63 skipped, 91% coverage**. The focused
  Phase 3 controls/integration also passed with resource warnings treated as
  errors. Ruff format/check, strict Pyright and architecture contracts pass.

Live ledgers are retained under `.loopforge/qualification/phase3-live` and
`phase3-live-clean`. Consolidated logs, snapshots, accounting and source hashes
are under `.loopforge/qualification/phase3/`; the review patch against the
preserved Phase 2 source baseline is `.loopforge/supervisor-phase3-review.patch`.

See [ADR-0019](../decisions/ADR-0019-repository-investigation-and-task-preparation.md)
and [the repository-goal operator guide](../architecture/repository-goal-supervisor.md).
The supported path remains one local worker, a narrowed assignment, approved
installed environments and trusted behavior probes. Production source sharing,
service migration, acceptance, push, merge, deployment and overnight rollout
were not enabled. Phase 4 is next.
