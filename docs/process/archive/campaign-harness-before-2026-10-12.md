Historical architecture snapshot, superseded by docs/architecture/campaign-harness.md.
Not current operating instructions.

# Sequential repository campaign harness

The harness coordinates a sustained review campaign over many bounded LoopForge
runs. Local n8n drives an outer investigation loop and a nested polling loop.
An operator starts or resumes the campaign; a separate HomeOS timer schedules
operator monitoring rather than starting additional campaigns.

Only one unfinished repository campaign may exist in a ledger. A transactionally
reserved execution slot permits one worker at a time. A process lock permits one
service per ledger; every n8n workflow must use that same service and ledger.
This governs harness workers, not unrelated agents or Ollama clients on the host.
Use one installation; separate ledgers are separate installations.

## Start the service

From the LoopForge checkout:

```sh
.venv/bin/python -m loopforge.entrypoints.campaign \
  --data-dir .loopforge/campaign --port 8124
```

Installed packages also expose `loopforge-campaign`. This is an unauthenticated
trusted-operator service bound to `127.0.0.1`. On Docker Desktop, n8n reaches it
at `http://host.docker.internal:8124`. Do not expose it publicly.

## Select the retained scope and create a campaign

Investigation runs are read-only. Use a local Git checkout and its full commit SHA; neither
dirty working-tree files nor changes arriving later are included. Creating a
campaign materializes tracked text blobs from that commit, without running
repository code. Include the governing documents explicitly. Git symlink blobs
are read as text, never followed to the host filesystem. `.env*`, private-key
extensions, binaries, and unselected files are excluded. Files over 150 KB fail
snapshot creation rather than silently pretending coverage; narrow the scope.
The snapshot ceiling is 30 MB and 5,000 text files. Treat tracked source and
reports as potentially sensitive; the service and Ollama stay local.

Example request (replace the path and SHA):

```sh
curl -s http://127.0.0.1:8124/campaigns \
  -H 'Content-Type: application/json' \
  -d '{"repository":"/absolute/path/helicops-cloud",
       "project":"helicops-cloud","revision":"FULL_40_CHARACTER_COMMIT_SHA",
       "scope":["contracts/src","services/policy/src","services/api/src",
                "edge/src","LAUNCH.md","README.md","docs"],"rounds":3}'
```

For Brook use `project: brook`, select the live app/core/migration/test surface,
and include `docs/product/brook-canonical-migration.md`. Avoid treating its
`archive/` as current implementation. The operator owns the scope; repository
instructions and model output cannot widen it.

The response contains a `campaign_…` ID. Creating a campaign does not start
inference. Get compact status at `/campaigns/{id}` and the durable evidence
ledger at `/campaigns/{id}/ledger`. Run events live in `runs.sqlite`; campaign
events live in `campaigns.sqlite`; pinned source is in the snapshot JSON.

## Connect the two n8n loops

Import `integrations/n8n/bounded-unit.json` first, then
`integrations/n8n/campaign-driver.json`. Both import inactive and have no
schedule or webhook. The parent references child ID `lfCampaignTask01`; if the
n8n UI changes the imported ID, select the imported child in Run Bounded Unit.
Set the campaign ID in the parent's Campaign Config node and execute manually.

The outer loop reads the checkpoint's `runnable` flag, dispatches one unit, waits for the child,
and returns for the next unit. The child claims or reconnects to work, waits
two seconds, and polls until that unit is no longer running. The parent waits one
second between units. Persistent dispatch keys reconnect HTTP retries to the same
task, including after completion. HTTP failures retry up to three times and then
stop the n8n execution. Restart the parent to reconnect after an interruption.
Stopping n8n alone does not cancel the bounded LoopForge worker.

The preserved legacy traversal uses these angles for each selected scope prefix;
focused issues now take precedence and stop at their issue checkpoint:
map → bugs → boundaries → contracts → docs → challenge → reconcile.
Workers can read the full selected snapshot for cross-component evidence; the
prefix is the focus, not a separate permission grant. Recent reports (the last
four, up to 12,000 characters) feed the next run as lower-trust hypotheses. All
older reports remain in the ledger. This bounded context is not an automatic
guarantee that every old unresolved question is revisited.

## Authority, evidence, and budgets

Review tools list files, read/search pinned source, replay observed excerpts,
read prior reports, and submit a report. There are no source-write, shell, Git mutation, or fan-out tools.
LoopForge owns every model turn, action authorization, budget debit, and stop.
Repository and previous-model observations enter context as untrusted content.
Every tool's risk, permission, retry, idempotency, approval, timeout and
sensitivity metadata is defined in code.

Reports use a strict schema. Findings can be suspected, rejected, uncertain,
or planned gaps. Evidence quotations must appear in source read during that
run. A model cannot promote a finding to confirmed or reproduced. Accepted
reports mean the schema and quotations were checked; they do not mean the
repository is clean or that a bug was reproduced. The ledger records which
files were read, scope, angle, SHA, runtime ID, outcome, and unresolved checks.
No repository tests execute in an investigation workload. Coding and patch-review
workloads run the operator-defined checks in the container described below. This is a
read-only capability boundary, not a claim of strong process isolation.

Each run defaults to 96 iterations, 500,000 cumulative tokens, and 7,200 seconds
(two hours); `model_timeout_seconds` defaults to 600 seconds per provider request.
Operator configuration may raise these to at most 192 iterations, 2,000,000
tokens, four hours, and 1,200 seconds per request. The active context remains
24,000 tokens; cumulative tokens are not a context-window setting. Coding tasks
default to five attempts, with five as the hard maximum. Limits are immutable
for that run; stored campaign contracts retain their explicit limits on restart.
These are per-run limits, not a total campaign time or token allowance.
Elapsed/token limits are checked at runtime boundaries; an in-flight provider
request can overrun them. Rounds and scope are finite. The end is an operator
checkpoint, never an automatic claim of convergence. Model failure or budget
exhaustion stops the campaign in `attention` without advancing its cursor.

## Pause, resume, recovery, and stage gates

Post operator commands with a nonempty evidence/reason string:

```sh
curl -s http://127.0.0.1:8124/campaigns/CAMPAIGN_ID/operator \
  -H 'Content-Type: application/json' \
  -d '{"command":"pause","evidence":"Review the current findings first"}'
```

`pause` during a run takes effect after that bounded unit exits. `resume`
resumes paused work or retries a failed unit at the same cursor with a new
LoopForge run. Existing terminal runs never reactivate. `complete` ends a
checkpoint/paused/attention campaign with the operator's recorded evidence;
this can also document abandonment, so it is not a correctness certificate.
An operator may `park` a ready, paused, attention, or checkpoint campaign
when its worker and execution slot are idle. Parking preserves its cursor,
reports, candidates, and stage; it is not a completion or correctness claim.
Another repository campaign may then be created. A parked campaign cannot
execute or enqueue coding. `resume` restores its checkpoint/ready status only
when every other campaign is parked or complete, preserving one active repo.
Parking a live worker or an occupied slot is rejected; pause/recover first.

A crash leaves the slot occupied without automatic expiry. After restarting
the service, post a reason to `/campaigns/{id}/recover` using the same request
shape. Recovery refuses while the worker is alive, cancels any linked run
through LoopForge's event path, and marks the interrupted unit for attention.
Then explicitly resume it. If persistence fails, ownership remains occupied.

For HelicOps, Stage 1 covers the retained foundation and its documentation,
including defects not listed in LAUNCH.md. At the stage checkpoint, an operator
may `advance` with evidence and a **dedicated local launch branch**.
The harness refuses main/master/staging/dev and verifies that the pinned
baseline is an ancestor of the branch. After accepted hardening tasks, the
branch is created in the latest accepted candidate checkout if it does not exist.
Without accepted tasks, supply an existing branch in the original checkout.
Brook cannot advance to Stage 2.

Stage 2 executes scoped coding tasks only on that recorded branch. Without a
queued task it stays at a checkpoint; it cannot fall back to a repair prompt
against the original checkout.

## Coding, verification, and fresh patch review

Submit one operator-owned coding contract to `POST /campaigns/{id}/coding`.
Submission records the task but does not start it. The existing n8n parent or
`POST /campaigns/{id}/tick` drives it. Resolve that task before selecting another.

```json
{
  "objective": "Reproduce and fix the identified error path; update affected documentation",
  "kind": "bugfix",
  "branch": "hardening/error-path",
  "allowed_paths": ["services/policy/src", "tests", "docs"],
  "image": "sha256:REPLACE_WITH_THE_64_HEX_DIGIT_LOCAL_IMAGE_ID",
  "checks": [{
    "name": "check_regression",
    "argv": ["/usr/local/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "tests/policy"],
    "timeout_seconds": 120
  }],
  "evidence": "Ledger finding and concrete failing case",
  "max_attempts": 5,
  "max_changed_files": 12
}
```

Stage 1 admits `bugfix` tasks; Stage 2 can use `launch` tasks for the LAUNCH.md
spec, or fixes to that implementation. Stage 2's branch must match the recorded
advance command. Main/master/staging/dev are always refused.

The image must already exist locally and is selected by immutable image ID;
Docker health and image availability fail before any model execution. Build
images and install dependencies outside the agent run. No automatic pulls or
dependency installation are performed. Checks use absolute in-container
executables and fixed argv, never a model-proposed shell command. The adapter
requires filesystem/network isolation, filtered environment, process timeouts,
bounded output and resource limits. It shares a kernel and does not claim
kernel isolation. Each check has a read-only workspace mount and no network,
with writable bounded `/tmp`, dropped capabilities and no new privileges.
Use checks whose caches/output go to `/tmp`.

Each task receives a separate local Git checkout with independent objects and
metadata, a dedicated branch, and no remote. The original checkout is never
mounted or edited. This deliberately avoids linked worktrees whose `.git`
pointer could lead outside the container mount. File edits are constrained
before execution; traversal, hidden paths and writes outside `allowed_paths`
are refused. Existing tests, check scripts and manifests are immutable. New
regression tests are allowed in explicitly selected test directories.
Ignored directories are expanded to their files for patch inventory, review,
and acceptance; a repository's gitignore cannot conceal candidate changes.

The coding runtime iterates edits and checks within its immutable budget.
Campaign coding source reads return up to 200 lines and 3,000 characters,
with source provenance, explicit clipping notices and a `start_line` cursor.
These response limits preserve sandbox path checks and do not imply whole-file
coverage. A single exceptionally long line may remain partially displayed.
After passing, it releases the agent slot. The next tick starts a fresh
read-only model run for patch review. Review must read the diff and every
changed file; configured checks run again before success. Any source change
between coding and review is rejected. Reported concerns or unresolved
questions veto automatic readiness. Even a clean model review is evidence,
not proof; it stops at `patch_ready` for operator inspection.
Patch review receives the task objective and changed paths; its provider history
is bounded to 96,000 serialized characters so a modest evidence set can remain
available. General review and coding use the same 96,000-character append-only ceiling.
The adapter fails before inference at that ceiling instead of dropping an early
conversation turn and repeatedly invalidating the provider prefix cache.
These are character limits, not exact token limits; run budgets remain separate.
Investigation handoffs contain up to four recent, schema-valid report summaries,
newest first, within 12,000 characters. Failed runs without a report are omitted;
the handoff remains complete JSON. Each lane receives a concrete review objective.
Run ledger output records actual source excerpt line intervals separately from
model coverage claims. Neither metadata nor exact quotations establish semantic
correctness; compare claimed coverage and reproduction against the evidence.
Activity includes `ActionRejected` and its denial reason, including
`BLOCK_CIRCUIT_OPEN`. The review workload reads authoritative event projections
before inference and stops with `REVIEW_REPORT_CIRCUIT_OPEN` if its required
report tool is unavailable. Untrusted text cannot trigger this check. The
original circuit threshold and immutable runtime events remain unchanged.
An open report-submission circuit cannot be repaired by
resubmitting in that run. Quote diagnostics identify every invalid citation by
finding/evidence index, with bounded hints for already-observed excerpts; they
never normalize quotations or accept unobserved evidence.

Read task status, checks, patch, model report and run IDs at
`GET /campaigns/{id}/coding/{task}`. Post `accept`, `retry`, `request_changes`, or `reject`
with a reason to `/campaigns/{id}/coding/{task}/operator`. `accept` checks that
the candidate content matches what was verified/reviewed, then commits it
locally. Duplicate acceptance reconnects to the accepted outcome. No merge,
push, production migration or deployment occurs. Accepted task commits become
the immutable baseline for the next task and subsequent investigation rounds;
changed accepted HEADs fail closed. Rejected candidates remain available for
inspection and are not incorporated into that baseline.

Failed coding/review runs put the campaign and task in `attention`. Inspect
the evidence, issue `retry` on the task (within its separate edit/review counters),
then `resume` the campaign and restart the n8n parent. The same candidate patch
is retained; each retry creates a new run rather than reactivating a terminal
run. Earlier failure and reviewer evidence is available through the read-only
`review_feedback` tool as lower-trust context. Crash recovery cancels a linked
run through domain events, retains the
candidate, and marks the task for attention. Each run has its own ceiling and
shares the immutable task's aggregate token/time limits and edit/review counters.

`retry` after a review failure reruns review of the existing patch. To correct a
semantic defect, use `request_changes` (Hub: **Return for edits**) from
`patch_ready` or review-stage `attention`. Both edit and review allowance must
remain. This queues another edit under the same contract and counters, then a
fresh review. Active, accepted, rejected, and exhausted tasks deny this command.
Revision runs pin their starting candidate identity and cannot succeed on that
unchanged candidate, even when its fixed checks already pass. Reverting to the
starting candidate also denies completion. A changed patch still must satisfy
all scope, fixed-check, and independent-review gates; change alone proves no fix.
Operator evidence is supplied as lower-trust feedback, without expanding edit
paths or runtime authority. Independent failing evidence blocks acceptance even
when a model reviewer reports no findings.

Reporter packets retain each observed source line once, removing overlapping
read duplication before serialization. Derived citation IDs only identify
subranges of already observed excerpts; existing IDs remain stable, unread
gaps remain absent, and required coverage is still checked independently.
Packets that genuinely exceed the unchanged reporting envelope still fail.

Investigation checkpoints apply to nonterminal issues. Queued investigations
dispatch sequentially; investigated/attention issues need an operator decision.
Once all investigations are closed or rejected, retained history does not block
the broader review stream. Closing an investigation records a completed
assessment, not a claim that a reported defect was fixed. Operator evidence
must retain deferred or exhausted repairs as unresolved backlog.

Brook's first README mapping unit has a code-owned document-role target and
explicit source intervals for README direction/reading order, the canonical
plan introduction, and the build handoff introduction. The current snapshot
needs five mandatory source pages within the existing ten-read allowance.
Missing intervals deny completion; the same intervals are included in the
controller objective and reporter packet. This bounds a mapping pass without
turning it into a full plan audit or interpreting planned features as bugs.

The harness does not automatically choose arbitrary findings, grant new edit
paths, or invent launch tasks. The operator supplies those contracts from
review evidence/the spec. Checking a fix can reproduce an issue using a
targeted regression command; existing tests cannot be rewritten to fake green.
The operator still needs to assess whether the chosen checks adequately cover
the reported bug and allowed/denied behavior. A production-only failure that
cannot be exercised without credentials remains unresolved.
## Live campaign monitoring

The HomeOS operator timer runs every 15 minutes as the macOS LaunchAgent
`local.loopforge-monitor`, queueing a follow-up into the original Codex thread.
Operational instructions and the process journal are in
`~/ai-platform/infra/campaign-monitor.md` and `campaign-process-journal.md`.
Heartbeat, queue acknowledgement, and completed agent review are recorded
separately in `.loopforge/campaign/monitor/`; a queued message is not proof of
a completed quality check. `local.loopforge-campaign` supervises port 8124.

Review prompt version 6 asks for 4–6 targeted source excerpts and at most two
findings. Missing coverage belongs in `unresolved`, which may contain up to six
next checks; it is not an unsupported high-severity defect. These are pacing
and submission constraints, not coverage or quality guarantees. New review
attempts use `think=false`, `num_predict=2048` (3072 for independent patch review),
and an explicit Ollama `num_ctx=32768`. Coding enables Qwen thinking with
`num_predict=8192`. If a thinking call exhausts this output allowance without
any tool action, the provider adapter records the charged failure and disables
thinking for subsequent calls in that run. The output ceiling and all runtime
authority remain unchanged; other runs keep their configured profile.
Review controllers and the bounded reporter retain their
separate non-thinking profiles. Reasoning and answer tokens share the output
ceiling and remain charged to the original aggregate task budget.

Campaign investigation conversations retain append-only history within a 96,000
serialized character ceiling and an additional conservative complete-request
UTF-8 byte bound including tools, framing and output reserve. Finalization uses a
separate bounded evidence packet and preserves validation feedback across retries. Existing generic adapter callers retain the old trim default;
campaign wiring explicitly chooses fail-on-overflow. Overflow fails before the
next provider request instead of repeatedly changing the cached prefix. The
character ceiling excludes the separately serialized fixed tool catalog and is
not an exact token count. Persisted evidence/events remain available. Work that
needs more context must use smaller follow-up units, within existing authority.

`read_file` still returns at most 200 whole lines and 3000 raw source characters.
`search_source(prefix, query)` searches literal text only in the pinned, filtered
snapshot and returns at most three matching excerpts, each charged against the
same configured successful-source-read allowance (legacy default ten; focused
issue default twelve; coding patch review default64). Capped results say more matches may
exist. No match says only that the configured prefix has no literal match; it
never establishes repository-wide absence. It has the same code-owned read-only
metadata as other review tools and cannot execute source or access host paths.

Source observations carry stable, run-local `excerpt_id` values and numbered
source lines. The structured finalizer uses
`{"excerpt_id":"e1","start_line":132,"end_line":135}`. Strict schemas reject
unknown fields and invalid ranges. Trusted code resolves only observed excerpts,
requires the entire span to lie inside that excerpt, copies the raw source text,
and revalidates the ordinary report schema and exact quotation constraint.
Each materialized quotation remains limited to1000 characters. Reference IDs
cannot retarget, reach unread files, or expand source coverage. Existing literal
quote submissions remain supported on the same report tool/circuit for backward
compatibility. `observed_excerpt` replays observed evidence without spending a
new source allowance. Source/reports remain lower-trust evidence throughout.

The new default model is `qwen3.6:35b-a3b`; the previous 27B identity remains
supported for existing configs. To select a different supported model for
**future attempts**, an operator may post:

```json
{"command":"model_changed","model":"qwen3.6:35b-a3b","evidence":"Measured local performance and bounded review quality probe"}
```

This requires a paused, attention, checkpoint, or parked campaign, an idle worker and
no occupied execution slot. The append-only campaign event supplies the
projected config; the original stored config is not edited. The command does
not resume work, move the cursor, alter budgets/permissions/coding contracts,
reactivate terminal runs or permit model/repository content to choose routing.
A completed campaign cannot change model. Resume is a separate operator action.

Performance evidence from 2026-10-10 is under
`.loopforge/campaign/monitor/performance/`. The opt-in sequential benchmark
`scripts/benchmark_campaign_models.py` compares short, cold review, append-only
follow-up and shifted-prefix requests on a stopped campaign. It uses no Hermes
model and runs no repository code. Measured 27B generation was12.6tok/s short
and9.8tok/s at7137 input tokens; 3.6 measured48.1 and29.9 respectively. Cold
review wall time fell78.6→16.1 seconds. For3.6, append-only follow-up input
processing was0.50seconds versus10.57seconds after shifting the prefix. A
four-action live harness probe used targeted source search and reference-based
reporting, completed in15seconds with no failed submissions, and produced exact
source quotations. This is a narrow known-issue probe, not a whole-campaign
throughput, patch acceptance, or model-quality guarantee. The campaign remains
stopped until explicitly resumed after diagnosis.

Repository switching preserves a parked campaign as a resumable event projection.
The single global execution slot, process lock, and other-campaign resume check
remain enforced. Only campaign metadata changes; terminal runtime streams never
reactivate. The hub displays parked state and excludes it from ongoing counts;
server-side checks reject resuming it while another repository owns the campaign.

Pinned snapshots admit source blobs up to500,000bytes to include Brook's generated
API types and versioned prototype, while retaining the30MBaggregate/5000file caps,
secret-file filtering, binary rejection and3000character review excerpts. Oversized
blobs still fail before model execution. A larger snapshot blob is not permission
to put its full content in provider context. Unit tests cover accepted bounded
contracts and oversized denial. Brook review objectives explicitly name
`docs/product/brook-canonical-migration.md` and `docs/product/BUILD_HANDOFF.md`;
the old methodology plan and dated deployment snapshots are historical context.

Review responses expose the remaining source-excerpt allowance and direct report
submission when it reaches zero. If repeated invalid read/search requests open
a source circuit, the review stops before another inference call; report-circuit
opening likewise stops. These guards use authoritative event projection, never
repository/model strings, and preserve failed-attempt evidence.

The newest validated operator resume evidence is included in previous_reports
as explicitly untrusted review feedback (up to4000characters, sharing the
12000character handoff bound). It supplies source-backed corrections to verify,
never runtime authority. Oversized referenced quotations identify the exact
finding/evidence, excerpt path/range and character count so corrections can
shorten that entry without relaxing the evidence limit or failure circuit.

Malformed report JSON is rejected with its syntax position, nearby text and a
minimal valid example. The tool never repairs or accepts a partial object; the
existing report circuit and strict schema remain in force.

Coding review_feedback includes immutable required-check definitions, recent
failure/retry evidence and the current verification failure. Diagnostic text stays
explicitly untrusted and bounded; retry handoff excludes raw patch text and unknown
policy fields. Coding checks retain up to 2000 sanitized characters per output
stream (the generic repair verifier defaults to 300). Fixed definitions remain
protected. An opened required-check circuit stops coding inference using the
authoritative event projection, preserving the candidate and original task cap.

The patch review verifier exposes the existing final acceptance requirement
before runtime success: no unresolved task blockers and only rejected hypotheses
(or empty findings). Successful changes belong in the summary. A runtime report
with open questions cannot silently become an accepted task; original task caps
and operator acceptance gates remain unchanged.

Brook first-round docs-scope docs/challenge/reconcile units use operator-maintained
precise source pairs in place of the broad grounding objective. These bounded
review targets affect investigation focus only. They use the latest one prior
report plus untrusted operator evidence; other units retain the normal handoff.
Permissions, run budgets, evidence validation and acceptance gates are unchanged.

Reference diagnostics report every invalid finding/evidence entry, its actual
observed source path and allowed range, and the observed reference catalog. No
unknown IDs, out-of-range spans or oversized quotations are remapped or accepted.
The model must correct its submission against the original evidence.

Report union validation failures now expose a compact diagnostic for the nearest
strict format, with field paths and explicit instructions not to mix quotation
and reference fields. This chooses error wording only: both original strict
schemas, exact observed-quote checks and the failure circuit remain unchanged.
Legacy prompt v11 required nonempty next_check for rejected hypotheses as well;
the current structured finalizer retains that strict schema requirement.
Hermetic tests reject mixed fields and empty next_check, then accept valid
corrections in both formats.

Tool-result context identity now includes the authoritative ToolSucceeded/ToolFailed
event sequence projected by the reducer. Equal failure text from distinct executions
therefore remains distinct at the provider's append-only boundary. Rebuilding the
same result keeps the same identity, and rejection without execution does not
create a new result. Source/tool observations retain their original trust labels;
review observations remain untrusted. Old event streams replay this projection
without rewriting history or expanding permissions, retry budgets or circuits.


## Reliability rollout and focused issue contracts (2026-10-10)

The primary unit is now a focused issue, not a position in the historical review
matrix. The old cursor remains a historical report count; it is not a closure or
accepted-fix metric. Existing rejected/exhausted coding contracts are preserved.

`POST /campaigns/{id}/issues` accepts an objective, operator evidence basis and
one to eight required `{path,start_line,end_line}` intervals. Optional immutable
limits default to three attempts, 250,000 known tokens, 900 seconds aggregate
active-run time and 12 source reads per attempt. The controller binds the selected
accepted source revision; a later source change stops the old issue instead of
silently reinterpreting its obligations. Source and task feasibility are checked
before an issue can be queued. Mandatory long lines or unavailable input fail.

Issues use append-only events: queued → running → investigated or attention.
`investigated` means a bounded report passed source/coverage checks and has no
blocking unresolved items. It does not prove a runtime defect, a clean repository,
or an accepted repair. Partial reports and their evidence remain in attention.
`POST /campaigns/{id}/issues/{issue}/operator` accepts retry, reject or close with
operator evidence. Only an operator can close an investigated issue. Retry retains
known token/time consumption and attempt caps; closed/rejected issues cannot run.

The outer n8n dispatcher uses a stable child-execution dispatch key:
`POST /campaigns/{id}/tick` with `{"dispatch_key":"n8n:123"}`. Retrying this request
reconnects to the same task even after completion. Three bounded HTTP attempts
are permitted. Legacy bodyless clients retain active-slot reconnection but must
not configure automatic retries across task completion. A failed n8n execution
does not cancel its LoopForge worker. Reconnect by reading authoritative state;
recover only after the owner has exited. The slot never expires automatically.

`pause` finishes the current unit; `cancel` requests cancellation between runtime
steps. A synchronous model request can remain pending until its timeout. The slot
stays occupied until the worker acknowledges completion. Recovery cancels linked
runtime streams through events and records known issue usage, then releases the
slot atomically with campaign failure. Issue completion/slot release is also one
transaction. Model generations are serial, including report finalization.

Native Ollama tool results use `tool_name`. The live review protocol exposes
`finalize_report` with no arguments; a separate native JSON-schema request writes
at most two findings using observed excerpt IDs. The host materializes quotations
and validates each against an actual contiguous observed excerpt. The authorized
objective travels separately from lower-trust evidence. Failed generations record
`ModelAttemptFailed`; known usage is debited before retry/stop, including malformed
message envelopes. Unknown usage is shown as unknown, not estimated as free work.
Finalization checks remaining run allowance before making its second request.

Candidate snapshots apply the same confidential-file exclusion policy as ordinary
review. Deleted files retain baseline content; changed binary, oversized, excluded
or unsupported symlink inputs fail explicitly. Actual diff/source intervals must
be read before patch review passes. Review capacity is checked before selection
and against the candidate; a large candidate must shrink or stop for decomposition.
Pinned snapshots batch Git blob reads, validate identities and sizes, and preserve
whole text. Outgoing request bytes, schemas, framing and output reserve have a
conservative context ceiling. The campaign provider removes older complete
action/result turns when needed to fit that same ceiling. A code-owned notice
tells the model that observations were omitted and must be reread when needed;
the original objective, latest complete turn and current blockers/check failures
remain. No model summary replaces evidence, and trust labels remain unchanged.
The immutable tool catalog and output reserve are charged before dispatch.
Required prompt/tools/latest-turn content that cannot fit stops before inference.
Durable run events and independently tracked source coverage are never removed.
Future coding-attempt feedback includes the actual stop summary and latest check
failure; the bounded handoff keeps the newest evidence rather than its oldest
prefix. This does not grant the model access to every historical event by default.

Patch-review obligations are exposed before report submission, including the
actual diff. Six controller turns without a new observed source reference end
investigation: complete mandatory coverage moves to bounded schema reporting;
missing or unspecified mandatory coverage stops with `REVIEW_NO_NEW_EVIDENCE`.
New source evidence resets the counter. Reporting validation retains its existing
bounded retries. This phase rule does not certify relevance, clear unresolved
questions, run checks, or accept patches; those gates remain independent.

New coding contracts have separate edit and review counters (default five/three),
an aggregate known-token ceiling (2,000,000), aggregate elapsed ceiling (21,600s)
and independent review-read allowance (64). Environment preflight occurs before
consuming an edit attempt. A failed review retries the unchanged verified candidate;
any candidate change invalidates that evidence. Three identical automatic-check
failures on an unchanged candidate, or unavailable checks, stop inference for
operator attention. Reads alone do not count as failed automatic checks. Failed
fixed commands retain bounded stdout/stderr. Existing check integrity, confinement,
container capability requirements and human acceptance remain mandatory.

Coding also tracks actual patch identity and unique successful read observations.
Eight turns without a changed patch or new source evidence stop with
`CODING_NO_NEW_EVIDENCE`; new source or a changed patch resets this counter.
This is separate from automatic-check repetition: the normal runtime skips checks
after read-only actions, so cached-read loops do not accrue its verification stall
strikes. A compact completed-read record travels as untrusted context bookkeeping,
never source truth or additional authority. Explicitly scoped colocated repository
tests and new permitted regression files can be changed; existing protected test
directories and fixed checks remain immutable. The prompt reflects those actual
path rules rather than declaring all repository tests protected.

The Hub displays issue state, reports, required intervals, remaining allowances,
known/unknown model consumption and cancellation pending status. Its elapsed number
is recorded event wall time, not measured GPU utilization or model-only time.
Raw qualification exchanges include actual provider prefill/decode/load timings.
A valid source quotation or agreement between models is never proof of a defect.

Migration: keep historical campaign and run events unchanged. New optional schema
fields use bounded defaults when replaying legacy contracts; this does not reopen
closed tasks. Use new issues only for distinct evidence-backed objectives. Do not
rename an exhausted task to evade its original caps. `ModelAttemptFailed` is an
additive event type: deploy the updated codec before opening new streams in an
older runtime. Existing event streams replay without rewriting history.

Generic discovery reviews use a bounded sampling contract. After their existing
source-read allowance is spent, trusted orchestration invokes reporting directly
instead of allowing repeated denied reads or cached-excerpt loops. Reports retain
a code-owned summary limitation: unread or omitted source is outside coverage.
This is a completed sample, not certification of a unit or repository. Issue and
patch reviews with mandatory intervals do not receive this fallback.

Reporting packs UTF-8 evidence, schema and the authorized objective within the
existing 26,000-byte envelope. It first explicitly omits repeated lower-trust
context and earlier reports. Generic samples may then omit whole observed
excerpts, recording the omitted count; the original observed evidence and stable
IDs remain available in run history. Mandatory source evidence is never removed
to fit. A packet that still cannot fit fails permanently before provider dispatch,
preventing retries of a deterministic sizing failure. No context window, runtime
permission, read allowance or immutable run budget is expanded.

Reporter source entries expose explicit original start/end lines and numbered
text, so citations do not require counting offsets through prose. These labels
are presentation only: IDs continue to resolve to exact original source, and
wrong IDs, out-of-range lines and oversized quotations remain invalid.

The first Brook README documentation pass compares the API-key quickstart with
the authentication implementation and valid-key/fail-closed regression source.
Its mandatory source intervals require four pages within the existing ten-read
allowance. This avoids substituting another plan-status survey for an actual
documentation-to-implementation comparison. Source tests establish intended
behavior, not executed test results. Reporting entries retain stable numeric ID
order, explicit original line labels and strict citation validation.

Ollama may return HTTP 500 after generating a malformed native tool call, rather
than an infrastructure outage. The adapter recognizes only a bounded, strictly
parsed error category and emits MODEL_TOOL_PARSE without exposing provider text.
The next attempt receives fixed protocol feedback in that run's conversation;
no partial tool call executes. Generic errors retain their existing classification.
Retry caps, context/output budgets and tool permissions stay unchanged. Missing
provider usage remains unavailable rather than being fabricated as zero cost.

The first Brook AGENTS.md mapping pass observes its API boundary statement,
the actual BFF path allowlist/traversal guards and denial-test source. Three
required pages fit within the existing ten-read allowance. Repository guidance
remains untrusted evidence, not runtime authority; the backend health endpoint
need not be proxied through this narrower BFF.

Explicit bounded campaign reviews may opt into `finalize_when_covered` before
the run starts. Its default is false. Only a code-owned precise target with
nonempty sufficient-evidence intervals enables it; complete actual coverage
then transfers directly to reporting. Missing intervals never trigger this
handoff, and generic discovery, issue and patch workloads keep their existing
controls. Reporting still enforces schema, citations, envelope and remaining
allowances. Target instructions name the actual finalize_report tool and no
longer prescribe an arbitrary number of source reads after coverage is complete.

The first AGENTS documentation pass compares the guide's directory and entry
route descriptions with apps/api/main.py, the dashboard root redirect and the
existing /rights audience branches. A repository rename is not evidence that
monorepo app directories disappeared. Application entry and product surfaces
are distinct source questions; no deployment verification is inferred.

Reporter citation selections now use a strict `start_line` plus integer `line_count` of 1–3, rather than a model-selected unbounded end line. Trusted code calculates the end line, then applies the existing stable-ID, original observed-range and 1,000-character quotation checks. This changes new reporter submissions only; stored range-based evidence and direct submission compatibility are retained. No clipping, ID retargeting, coverage relaxation or automatic finding acceptance occurs. The packet and remaining-allowance calculations use the actual short-selection schema.

## Audit remediation controls (2026-10-11)

New generic reviews select a precise source branch, matching failure-test interval, and a separate local imported producer/consumer where available. Python AST import edges, TypeScript relative imports and dashboard `@/` imports resolve only against the pinned snapshot. Test selection prefers exact sibling files, otherwise requires a resolved import of the selected source and an actual test-case definition. Shared basenames or incidental text mentions do not establish a relationship. Broad Python scopes prefer substantive code over metadata-only modules; Python anchors prefer executable AST branches and reverse consumers anchor the actual import. Explicit test directories prefer actual test-case files and anchor a case rather than setup helpers. Fixture-only scopes and explicit file focus remain reviewable. Modules without functions anchor module statements/declarations rather than descriptions. Dynamic/unresolved imports remain coverage limitations. Required intervals cannot be replaced by a related-file selection. Documentation reviews include selected dated/supersession continuations. Missing tests/consumers remain explicit coverage limitations, not fabricated assurance. The selection is a bounded heuristic; it does not establish repository-wide coverage.

Successful provider envelopes require both nonnegative integer usage counters. Missing/partial/malformed counters never produce an executable action or accepted report. Known controller usage is retained when a reporter fails with unknown usage; `REPORT_USAGE_UNKNOWN` explicitly identifies the unaccounted part. Unknown usage is not zero usage.

Finding assessments are append-only campaign events with revision, stable claim/path identity, disposition, evidence and next check. Changed dispositions require explicit supersession plus new evidence. Duplicate writes are idempotent. Context carries bounded latest assessments with historical revision labels and untrusted-evidence provenance. These records cannot change tools, checks, budgets or terminal tasks. Operator rejection of an investigation is distinct from fixing its underlying defect.

A review unit receives a durable `review_defined` contract before its next run: at most four attempts and aggregate token/time allowances equal to four original per-run allowances. Resume cannot reset the contract or erase failed runs. An unchanged failed implementation fingerprint cannot retry; corrective harness/provider changes permit another attempt only within remaining aggregate limits. Individual runs retain their existing ceilings. Historical units without such a contract receive one before their next run, with prior linked run consumption included.

Archived snapshot JSON is diagnostic only; every new review reconstructs and validates pinned Git blobs. Coding feedback packs whole records, discloses omissions and compacts oversized diagnostic values while retaining patch/check identity. No diagnostic text changes registered check authority.

The reporter separates observations from defect hypotheses. Suspected defects require distinct expected/observed behavior and two distinct exact evidence selections. Expected/observed basis is retained with the report. These structural requirements do not prove semantic entailment; independent source assessment remains necessary. Capacity is two findings, two selections each, and one to three lines per selection. Optional paired character offsets select exact characters from one observed line without clipping or retargeting. Read tools still refuse a single line exceeding their 3,000-character page envelope. Additional precise checks persist in the revision-pinned, paginated `/followups` proposal queue; they grant no execution authority and need operator qualification into a bounded investigation.

Activity defaults to 20 recent runs and 100 events per run; bounded offsets and exact campaign-owned run selection preserve historical drilldown. The projection distinguishes last durable elapsed time from current wall-clock age. A separate read-only request tracker exposes controller/reporter wait start and timeout, alongside active slot ownership. Request timeouts are clamped to remaining run allowance; synchronous HTTP cancellation still waits for completion/timeout and is not a hard interruptible wall-clock guarantee.

n8n parents start at most 12 units, with new starts only during the first 30 minutes. Parent timeout is six hours; child timeout is five hours, accommodating the existing four-hour maximum individual run without expanding agent budgets. The 15-minute monitor reconnects only a uniquely ready authorized Brook campaign with no harness worker/slot and complete empty n8n running/waiting inventories. A parent timeout never releases a worker slot. Ambiguous launch delivery stays protected by Hub launch idempotency.

Supervision checks have identities and queued/running/heartbeat/completed phases. Stale acknowledgements cannot clear newer checks or restart completed checks. Overdue delivery reconciles against the existing Codex daemon's Unix WebSocket and queue receipt; only a matching queued submission on an idle thread may be started, without resubmission. Missing/ambiguous receipts remain visibly unconfirmed. Timer heartbeats and queued messages do not establish a completed assistant check or promise activation under every failure. `--no-queue` performs observation only.


The Codex transport/handshake follows the [official App Server protocol](https://developers.openai.com/codex/app-server): the existing Unix control socket uses WebSocket frames and an initialize/initialized handshake. The installed protocol schemas supply the read/list/start method contracts; read-only reconciliation was tested locally against the running daemon.

Bounded-target prompts retain the selected code-owned lane guidance. Markdown intervals rotate across headings by round/lane and stop at section boundaries; dated/supersession notices remain separate required evidence. Documentation-only packets explicitly leave implementation and live state unverified. Repeated small-file packets and completed units do not establish independent verification; final coverage counts unique observed intervals separately.

Provider JSON Schema requires expected/observed basis fields on new short finding generation, matching runtime's conditional suspected-defect validator. Schema customization preserves the other required fields and compatibility for historical/non-suspected parsing. Empty findings remain valid; missing or empty suspected-defect basis still fails closed. Model schema definitions participate in implementation fingerprinting. Corrective schema changes cannot reset or reactivate an exhausted investigation.
