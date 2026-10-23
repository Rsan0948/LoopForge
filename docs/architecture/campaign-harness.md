# Sequential repository campaign harness

LoopForge owns run state, tool authority, immutable budgets, evidence validation
and verification. Local n8n schedules bounded units, polls their checkpoints and
continues eligible batches. The HomeOS Automation Hub provides controls and
retains accessible results. The 15-minute monitor supervises quality and recovery;
it is not the scheduler for normal between-unit progress.

Current implementation evidence and restart gates are in
[the implementation results](../process/campaign-implementation-results-2026-10-12.md).
Historical rollout reports describe their dated versions, not current behavior.

## Ownership and stages

Only one repository may be runnable in a ledger. Other unfinished campaigns may
remain parked. One transactionally reserved slot permits one worker; one service
lock permits one harness process per ledger. Campaigns, Hub builder and local
qualification/benchmark tools also share a cross-process model lease. Unrelated
external clients that do not take that lease are outside this exclusion contract.

Brook is the current intended campaign; HelicOps remains parked. Both use the
approved A3B model. HelicOps Stage 1 hardens retained foundation code and documents
existing behavior, including issues outside LAUNCH.md. Stage 2 requires an
operator-recorded dedicated launch branch and scoped tasks. Brook has no Stage 2.
Repository/model content cannot grant a branch, edit scope, command or capability.

## Local services and installation

The campaign service normally uses port 8124 and `.loopforge/campaign/` for
`campaigns.sqlite`, `runs.sqlite`, candidate checkouts and source snapshots. Ollama
uses 11434, n8n 5678, and the Hub 8050. Keep these local/protected; source and reports
can be sensitive. No provider credential or repository secret belongs in a model
prompt. Local HTTP and a working directory are not strong isolation.

The Hub provisions an authenticated dispatcher from
`integrations/n8n/campaign-driver.json` and its bounded child template. The parent
references child `lfCampaignTask01`. The installed parent has an authenticated
webhook and stable launch identity. Controlled upgrades back up idle workflows
and Hub settings, preserve authentication/hook identities, verify both workflow
logics, commit their template hashes together, and leave workflows inactive.
Launching activates the child before the parent and verifies authentication denial
before submitting work. Do not manually overwrite an installed parent with the
manual template or bypass a template mismatch.

Uncertain provisioning or launch results require reconciliation. Unique matching
workflow identity or harness dispatch receipts can corroborate delivery; elapsed
time alone cannot. Uncertain execution or credential creation is not blindly
repeated. The Hub exposes attention instead of silently duplicating side effects.

## n8n execution

The parent reads `/campaigns/{id}/checkpoint`, dispatches one bounded unit through
the child, waits for completion, and returns to the checkpoint. The child polls
compact state every five seconds; the parent waits one second between units.
The parent bounds each batch by twelve units and thirty minutes. If work remains
eligible, an authenticated continuation starts another batch with the same launch
ID. An operator decision, attention state or parked campaign stops continuation.

Dispatch keys combine launch and current unit identity. Coding identity includes
the phase and edit/review counters; completing coding cannot accidentally reconnect
the next review to the old dispatch. A stale unit key is denied before reservation.
Retries reconnect to the same task. Stopping n8n alone does not cancel an in-flight
LoopForge request. Successful orchestration payloads are not retained repeatedly
in n8n; LoopForge events and Hub artifacts remain the authoritative outputs.

## Review tasks and evidence

The retained traversal uses map, bugs, boundaries, contracts, docs, challenge and
reconcile angles, with focused issues taking precedence. Planning chooses pinned
source intervals and less-inspected coverage. Documentation work pairs an explicit
claim with likely implementation and discloses unresolved pairing. It does not
interpret a future plan or dated historical document as a current contract.

A task's actual reader and complete reporter serializer preflight mandatory
coverage before a review attempt is reserved. Instructions, schema, escaping,
objective and bounded retry feedback count toward capacity. Inaccessible or
oversized mandatory evidence is denied for decomposition. A source snapshot's
file-size allowance is not permission to send its entire contents to the model.

Source tools operate on the filtered pinned snapshot. Reads return at most 200
lines/3,000 characters with hash, empty-file status and usable line/column
continuation. A long line can be assembled over several reads; partial fragments
do not establish whole-line coverage. Empty files require an actual observed
empty excerpt. Literal review search charges returned excerpts against the same
read allowance. Capped search results never establish repository-wide absence.

The small navigation tool set includes a bounded task-ranked map and symbol
lookup. Python uses AST; other supported formats use conservative declarations
and static imports. Unresolved aliases and textual references are disclosed.
Navigation summaries guide reads; they are not source evidence. Coding content
search uses bounded result pages and explicit continuation; it does not search
filenames. Exact source comes from read_file.

Required evidence is fetched through normal journaled read actions. Preselected
workloads can finalize mechanically once their mandatory coverage is present.
Discovery may inspect more evidence within the original limits. The reporter is
a separate schema-constrained request. It receives literal observed file text,
including prose, as evidence; source content cannot issue instructions.

Reports support suspected, uncertain, rejected and planned-gap findings. Exact
references select observed excerpt IDs and original line ranges; short selections
contain 1–3 lines and at most 1,000 characters. Long-line offsets are zero-based
character selections. Trusted code copies and validates quoted source. Matching
scalar contracts are rejected as supposed contradictions when recognizable
without interpreting ambiguous conditions/units. Unknown claims remain hypotheses;
valid schema and quotations are not proof of semantic correctness or reproduction.

The report packet removes duplicate observed lines and optional context while
preserving mandatory evidence. It never clips required source to manufacture
admission. Whole prior records and retry feedback are bounded with explicit
omissions; full durable evidence remains available for inspection.

## Coding and acceptance

`POST /campaigns/{id}/coding` records an operator-owned task without starting it.
The contract fixes a dedicated branch, allowed paths, immutable local image ID,
absolute check argv, permissions, limits and evidence basis. Stage 1 admits fixes;
Stage 2 uses its recorded launch branch. Main/master/staging/dev are refused.
Images/dependencies must already be available. No automatic image pull, arbitrary
shell command or model-proposed check definition is admitted.

Each task uses an independent local checkout without a remote. It does not mount
or edit the original checkout. Hidden/traversal/out-of-scope paths are denied.
Existing tests, check scripts and manifests stay immutable; explicitly allowed
new regression tests can be added. Ignored files remain visible in inventory,
review and acceptance. Repository content cannot widen these contracts.

The coding context names the frozen base, allowed paths, fixed checks and success
condition. Failure previews point to full bounded feedback. Single-file multi-edit
validates the expected hash and every replacement before one atomic write; it
provides neither multi-file atomicity nor a host-writer compare-and-swap guarantee.

Checks run in the declared local container contract: read-only workspace, no
network, filtered environment, bounded writable temporary storage, dropped
capabilities, timeouts and resource limits. This shares a kernel and does not claim
kernel isolation. Unsupported required capabilities fail before execution.
Candidate changes must fit fresh-review read/report capacity before fixed checks.

Optional deterministic check reuse is explicit in the frozen check contract.
Receipts match workspace bytes/modes, command, image, environment, baseline and
effective timeout. They are one-use; failures, changed inputs and a new verifier
instance force execution. Fresh final verification cannot inherit coder receipts.

After coding passes, another bounded unit independently reviews diff and required
changed source, with fixed checks repeated. Unresolved blockers or non-rejected
findings veto readiness. The candidate identity must stay unchanged. The task
then stops at `patch_ready` for an operator decision. `accept` rechecks identity
and commits locally; it never merges, pushes or deploys. Accepted commits become
the next immutable baseline. Rejected candidates are retained but not incorporated.

Coding and campaign finish events plus slot release share one transaction. Recovery
can reconcile an already-durable coding outcome without redoing work or charging
another attempt. Terminal runs never reactivate. Otherwise recovery cancels the
linked interrupted run through events and retains evidence for attention.

## Budgets, profiles and retry

CampaignConfig and CodingTaskConfig define code-validated ceilings. Each run has
an immutable iteration/token/time limit; coding tasks also retain aggregate
consumption and separate edit/review counters. Cumulative tokens are not a context
window. Local inference removes API spend but still consumes time, memory and
finite attempts. No recovery, profile change or retry resets exhausted counters.

A3B uses a 32K provider context. Assembly has a separate heuristic allowance; the
adapter also measures the complete serialized request conservatively in UTF8
bytes, reserving output/framing. This is not an exact tokenizer. Stable prefixes
are retained while they fit; compaction drops complete older turns with an explicit
notice and current blockers. Mandatory evidence cannot be silently dropped.

`baseline-v1` remains the coding profile. The alternative Qwen sampling/thinking
profile requires qualification before promotion. Small validated thinking can be
replayed as model inference; oversized optional reasoning is omitted whole with a
notice. Malformed actions are never replayed. Output exhaustion without an action
can disable thinking on later calls in that same run without increasing its limits.
Review and reporting have separate explicit sampler profiles. The reporting profile
uses zero presence penalty and a concise literal-source contract. `/health` records
loaded source/template/model identities and effective role settings. Provider load,
prefill, decode and token metrics are retained when supplied; unavailable is not zero.

`retry` after a failed review preserves the candidate; `request_changes` queues
another edit and fresh review only if both counters permit it. Revision runs cannot
pass an unchanged starting patch. Failure-linked correction receipts are required
when a changed implementation authorizes another review attempt. They name the
actual failure and current loaded fingerprint; original caps remain unchanged.

## Operations and visibility

`/campaigns/{id}/checkpoint` is compact polling state. `/ledger?after=...&limit=...`
provides bounded durable history; `/activity` pages runs and events. `/dispatches`
corroborates launch delivery. The Hub provides server-side search, older history,
actual model/profile/baseline visibility, full execution downloads and archived
reports. Terminal execution previews are bounded and cached; full output remains
retained. Reading older campaign history holds refresh until the user returns live.

Pause finishes the current bounded unit before stopping. Parking requires an idle
worker/slot and preserves stage, cursor and candidates. Resume is refused while
another repository is active. Crash slots do not expire automatically. Recovery
refuses a live worker and fails closed if persistence cannot complete.

The HomeOS LaunchAgent schedules a check every fifteen minutes. Check identity,
queue delivery, active heartbeat and completed acknowledgment are distinct.
Ambiguous delivery/system errors require existing-check recovery, not duplicate
submission. Current operating instructions override historical chronology in
`~/ai-platform/infra/campaign-monitor.md`.

Backups preserve consistent ledgers, exact raw Git patches, modes and added files.
The isolated restore drill verifies bytes, reconstructs candidates and checks
restored API projections without executing repository workloads or dispatching.
Use qualification scripts and the documented restart gates before resuming after
infrastructure changes. A passing synthetic suite alone is not general repair proof.
