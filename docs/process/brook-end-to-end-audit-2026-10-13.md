# Brook local AI end-to-end audit — 2026-10-13

## Follow-up status — 2026-10-16

A03 is repaired in the local LoopForge harness: coding `search_files` now applies
the same code-owned `source_allowed` path predicate as direct reads **before**
opening candidate files. A synthetic allowed/denied regression covers `.env`,
`.env.local`, `.pem` and an ordinary source file. The focused search tests and
the 280-test campaign suite pass; Ruff, Pyright and diff checks pass. The idle
campaign service was reloaded and its task, issue, state and baseline projections
matched the pre-reload snapshot. This establishes the tested local boundary, not
a new Brook product repair or a full campaign-quality result.

A04 is repaired in the local monitor: runnable stage-1 checkpoints can reconnect
through the same native n8n idle guard as ready campaigns; empty checkpoints
remain idle. The monitor suite passes 13 tests. Docker Desktop and n8n were down
at this check-in and were restarted. Native running/waiting inventories are both
empty; production workflows remain inactive with successful-output retention on.

A01's mechanical auto-finalization was removed for generic and patch reviews;
the controller can now read additional evidence after required intervals, while
source-read limits and a repeated-read guard still terminate stale work. Patch
review rejects a repeat report without a new source read. A02 now requires new
coding contracts to name changed-file deliverables; the fixed verifier cannot
pass after a first green edit while a named regression file is missing. Legacy
terminal task records still load with their original contract and counters.
These are tested mechanics, not a claim that every future objective has been
fully encoded or that real-model discovery-to-repair quality has been proven.

A05 now ranks lexical definitions before textual references, bounds method
spans for navigation, and provides a capped reference offset. Local read-only
checks on the accepted Brook checkout located the previously missed definitions
of `createSupabaseServerClient`, `requireFirmRole` and `listRightEvents` first.
These lexical spans remain navigation hints that require exact source reads.
A06's reporter prompt now permits expected behavior from tests, callers and the
authorized task without inventing a documentation claim. Two isolated local
A3B synthetic reports found a test/implementation contradiction and rejected an
intentional 503 hypothesis; this is a narrow prompt check, not a broad quality
calibration. Its result is in `.loopforge/campaign/monitor/qualification/a06-20261016`.

The LoopForge suite passed 2,790 tests with 67 environment-dependent skips after
A01/A02/A05; the infrastructure suite passed 75 tests. The changed-file Pyright,
Ruff, format and diff checks passed. A07 remains open: the local loop still lacks
a trusted autonomous review-to-repair coordinator and finite Brook queue. The
real A3B discovery-to-repair canary and n8n transport path have not been rerun
against these changes. Do not launch a broad Brook pass or claim end-to-end
readiness on the strength of unit tests and two synthetic reports alone. The
original audit evidence below remains a historical pre-fix record.

Audit requested after reading `brook-campaign-handoff.md`. This report assesses
the currently installed harness and accepted Brook source. No implementation,
workflow, model, campaign-state or product changes were made. New files are this
report and local audit evidence. No live inference or campaign dispatch was run.

The principal obstacle is incomplete work and evidence handoffs. Generic review
currently jumps from mechanically selected excerpts to a report; coding can stop
before the full requested change is assembled; turning review output into scoped
repairs and deciding acceptance still depends on a supervising Codex session.
These are distinct from whether n8n successfully delivers and retains a run.

## Evidence and current state

Evidence directory:
`.loopforge/campaign/monitor/audit-2026-10-13-end-to-end/`.
`probes.py` contains offline reproductions with controls;
`probe-results.json` records their results; `campaign-tests.txt` records the
existing campaign test run; `live-readback.json` records sanitized local GETs and
Docker image presence. Probes use temporary fixture repositories, in-memory state,
fake model/check responses, and read-only access to the live campaign ledger.

At 2026-10-13 13:05 UTC, Brook was still checkpoint210, event1138,
`runnable=false`, with no worker or slot. Its accepted baseline remained
`9eba77fa4680348b4d767da73ddee1c82bba0cfc`. HelicOps remained parked6/event76.
Native n8n running/waiting inventories were both empty, with no further pages.
Both production workflows were inactive and retained successful execution data
with `saveDataSuccessExecution=all`. All six distinct Brook check images were
present locally. Image presence does not establish that every workload executes.
The installed A3B digest matched the recorded model identity; no model was loaded.

The loaded Python source hashes match disk. The overall release fingerprint does
not: its cached workflow-template hashes predate the retention update. This is
an identity/observability discrepancy, not evidence of stale Python code or broken
current retention. A later qualified release should refresh the combined identity.

The existing campaign suite passed **279 tests in 20.40 seconds**; the monitor
suite passed **12 tests**. Passing these suites does not invalidate the new
reproductions below. This audit did not rerun the whole LoopForge suite, real model
quality trials, product tests, or browser/deployment checks.

## A01 — P1: Automatic finalization prevents the review model from investigating missing evidence

**Reproduced offline and corroborated by retained production observations.**
`CampaignHarness.drive` assigns a target and mandatory intervals even for generic
workloads, then enables `finalize_when_covered`. `ReportingReviewModel` mechanically
reads those intervals and immediately calls the reporting model. The underlying
controller gets no opportunity to search for a missing method, test or consumer.
Patch reviews also explicitly enable this behavior.

The controlled probe supplied one selected implementation and an unread consumer.
With automatic finalization enabled, the next action was `submit_report`, with
zero controller calls. With it disabled, the controller made the expected consumer
read. Across 187 retained generic reports, 157 contain unresolved questions;
104 have provider observations, and their only recorded request kind is
`reporter-bounded-v2`. These are overlapping historical samples, not an estimate
of overall model accuracy or independent review coverage.

The real README planner still selected `docs/data_dictionary.yaml:177–199` and
the header of `scripts/smoke_rights_rpc.py:1–22`, with an empty limitations list.
It missed the actual rights route/store/RPC contract. This reproduces the known
pairing gap and explains why supplying navigation tools alone cannot resolve it.

Sources: [campaign.py](../../src/loopforge/entrypoints/campaign.py:898),
[campaign_review.py](../../src/loopforge/entrypoints/campaign_review.py:885),
[campaign_execution.py](../../src/loopforge/entrypoints/campaign_execution.py:103),
[campaign_quality.py](../../src/loopforge/entrypoints/campaign_quality.py:256).

**Fix gate:** keep mechanical fetching, then permit bounded investigation of
missing contract evidence within the existing allowance before finalization.
Do not simply remove the guard and reintroduce the old endless-read failure.
Test a necessary extra consumer, a complete packet that finishes promptly, a
missing/unavailable consumer that stays unresolved, and repeated-read termination.
An unresolved patch review must have a path to obtain evidence rather than merely
repeatedly regenerate its report from the same packet.

## A02 — P1: Coding success can occur before the requested regression test is written

**Reproduced through the real coding runtime with a fake deterministic check.**
The task requested an addition fix plus a new regression test. A scripted model
offered those two edits in order. After the first edit passed the registered
check, the runtime terminated `succeeded`; it made one model call and never wrote
the regression test. This is coding-stage success, not demonstrated full campaign
acceptance: independent review/operator acceptance remain later gates.

The runtime verifies after a write and stops on passing verification. The coding
verifier requires some permitted change and passing fixed commands, but there is
no explicit ready-for-final-check boundary or structured required deliverable.
Consequently, a source change plus tests/docs/refactoring task is only complete
when its fixed checks encode every deliverable. The textual objective alone does
not provide that guarantee.

Sources: [runtime.py](../../src/loopforge/application/runtime.py:980),
[campaign_coding.py](../../src/loopforge/entrypoints/campaign_coding.py:482),
[repair.py](../../src/loopforge/workloads/repair.py:249).

**Fix gate:** distinguish intermediate checking from completion. Require an
explicit completion action plus immutable deliverable/verification obligations,
or fully encode deliverables in the trusted task contract before admission.
Test a passing first edit with outstanding regression/docs work, completed work,
and an attempted completion with failed checks. Preserve fresh review and local
operator acceptance; do not relax checks to keep a task moving.

## A03 — P1: Coding search bypasses the source exclusion used by reads

**Reproduced with a synthetic marker, without real credentials or inference.**
In an isolated fixture, `read_file('.env')` was denied, but
`search_files('AUDIT_FAKE_CREDENTIAL')` returned the `.env` line and marker.
The campaign wrapper filters direct reads through `source_allowed`, while search
delegates to a workspace walk that skips Git metadata and symlinks but does not
apply the campaign's excluded-source predicate.

This is a demonstrated exclusion bypass, not evidence that a real Brook secret
was leaked. It becomes relevant whenever a prohibited file exists in a candidate
checkout, including repository content or locally introduced files.

Sources: [campaign_coding.py](../../src/loopforge/entrypoints/campaign_coding.py:377),
[file_tools.py](../../src/loopforge/adapters/file_tools.py:168),
[campaign_evidence.py](../../src/loopforge/entrypoints/campaign_evidence.py:14).

**Fix gate:** apply one code-owned source-admission predicate before searching,
reading, mapping or serializing content. Test ordinary allowed matches and denied
`.env`, key and excluded-directory fixtures. Filtering after observation logging
would be too late.

## A04 — P2: Monitor reconnection ignores runnable work at Brook's current checkpoint

**Reproduced with a fake HTTP requester and no actual dispatch.**
The campaign API permits queued coding/focused issues at a stage-1 checkpoint and
reports them runnable. The monitor only considers `status='ready'`. In the probe,
ready+runnable produced one simulated Hub launch; checkpoint+runnable produced
zero and the message `no uniquely authorized ready Brook campaign`.

The current empty checkpoint correctly stays idle. The failure matters after new
authorized focused work is queued there and its dispatcher needs starting or
reconnecting. Manual/authenticated Hub launch remains available; the automatic
reconnect path cannot handle this valid state. Existing monitor tests cover ready
campaigns but omit runnable checkpoint work.

Sources: [campaign_monitor.py](/Users/rubensanchez/ai-platform/infra/campaign_monitor.py:125),
[campaign.py](../../src/loopforge/entrypoints/campaign.py:1234).

**Fix gate:** reconcile the authoritative runnable projection with supported idle
states, retaining single-repository ownership and native n8n idle checks. Cover
queued checkpoint work, empty checkpoint, parked/attention campaigns, active
workers and ambiguous native execution inventory.

## A05 — P2: Symbol lookup loses definitions behind early references

**Reproduced on the accepted Brook source.** `find_symbol` returned 30 matches,
`capped=true`, and zero definitions for both `createSupabaseServerClient` and
`requireFirmRole`. Their definitions exist in `lib/auth/server.ts:19` and
`lib/tenancy/queries.ts:103`. Lexicographic traversal reaches numerous callers
before those files, and the function returns immediately at 30 matches with no
continuation selector.

`listRightEvents` returned 13 matches with zero definitions because its object
method syntax is not recognized. TS function definitions that are recognized
have an end line equal to their declaration line, so this is not a complete-body
lookup. Literal search and exact reads are fallbacks, but cost more model turns.

Source: [campaign_navigation.py](../../src/loopforge/entrypoints/campaign_navigation.py:108).

**Fix gate:** find/rank definitions before capped references, provide bounded
continuation, and represent unsupported syntax explicitly. Test these actual
Brook symbols, definitions after many callers, aliases, and method-body bounds.
Do not label textual matches as resolved semantic references.

## A06 — P1 qualification gap: The shared reporter assumes every defect is documentation drift

**Source-confirmed contract mismatch; model quality effect unmeasured here.**
The shared reporting system prompt requires `expected_behavior` to state the
CURRENT DOCUMENTATION claim. This reporter is used for generic bug/boundary
reviews and patch review too. Expectations for an async failure, compensation
safety, or an executable regression can instead come from tests, callers and the
authorized task. Requiring documentation creates an unnecessary reporting obstacle
or pressure to invent a prose contract.

The qualification fixtures mostly compare small documented constants. The real
Brook qualification is a useful seeded Unicode repair with its check environment
and source target supplied. Together they do not qualify discovery-to-repair on
async recovery, compensation, multiple files, or previously unknown evidence paths.
The earlier results report discloses this limitation; the green scores should not
be promoted into general campaign readiness.

Sources: [ollama_reporting.py](../../src/loopforge/adapters/ollama_reporting.py:41),
[qualify_campaign.py](../../scripts/qualify_campaign.py:29),
[qualify_brook_repair.py](../../scripts/qualify_brook_repair.py:133).

**Fix gate:** role-appropriate expected/observed evidence contracts, plus frozen
Brook cases for intended denials, async failure recovery, partial exports,
compensation/idempotency, multi-file completion and missing consumer evidence.
Grade the actual expected behavior and baseline-fails/candidate-passes checks.
Keep development and fresh held-out evidence separate. Use isolated qualification
copies; never reopen exhausted tasks under the guise of an evaluation.

## A07 — P1 workflow gap: The local loop has no autonomous review-to-repair closure

**Source-confirmed architectural dependency, not a broken authorization check.**
Generic units store reports and advance the cursor. Focused issues and coding
contracts enter through operator APIs; candidate acceptance is an operator command.
n8n repeatedly dispatches already eligible work. The monitor's prompt delegates
quality decisions and campaign driving to a hard-coded Codex thread.

This means a transport-successful local run can finish its entire review schedule
with unresolved questions and no newly accepted repair. The local model has not
been given a complete trusted process for selecting an actionable issue, obtaining
a reproducer, assembling a check environment and coding contract, and closing the
outcome. The original task caps and existing operator gates are intentional and
must remain intact.

Sources: [campaign.py](../../src/loopforge/entrypoints/campaign.py:920),
[campaign_execution.py](../../src/loopforge/entrypoints/campaign_execution.py:188),
[campaign_execution.py](../../src/loopforge/entrypoints/campaign_execution.py:489),
[campaign_monitor.py](/Users/rubensanchez/ai-platform/infra/campaign_monitor.py:14).

**Fix gate:** define a finite trusted coordinator workflow that owns the queue and
allowed transitions: evidence question → reproduction → admitted coding contract
→ checks → fresh review → delegated local decision or explicit scoped blocker.
Use code-owned templates/checks and the user's existing delegation; model findings
remain proposals, not permission to expand paths, budgets or execution authority.
Persist outcomes by defect/claim and revision so a rejected attempt cannot count
as a resolved product defect and repeated reports cannot count as new coverage.

## Recommended sequence

1. Repair source-search admission and the completion/investigation boundaries
   (A03, A01, A02), with meaningful allowed and denied cases.
2. Fix navigation and checkpoint reconnection (A05, A04); qualify reporter behavior
   on representative contracts (A06).
3. Supply a finite Brook work queue and trusted closure workflow (A07), with runnable
   check environments and clear local completion/blocker outcomes.
4. Run an isolated real A3B canary through discovery, reproduction, multi-step coding,
   fixed checks, fresh review and a recorded local decision using n8n. Verify full
   output retention and exact patch identity. The next canary should exercise the
   newly repaired boundaries rather than repeat only the one-line Unicode repair.
5. Resume authorized Brook work against the accepted baseline under existing
   immutable limits. Keep old rejected/exhausted tasks and authorship intact.

The known upload, ICS and logout defects remain unfixed. This audit adds no product
acceptance, no budget reset, and no launch-readiness claim.
