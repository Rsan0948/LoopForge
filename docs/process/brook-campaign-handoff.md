# Brook campaign handoff

Last verified: 2026-10-16. This is the short operational entry point for a new
chat. Recheck every live assertion before acting; the monitor and campaign API
are authoritative for current state.

The [deeper end-to-end audit](brook-end-to-end-audit-2026-10-13.md) found seven
additional harness gaps. A01-A06 received local mechanical/prompt changes on
2026-10-16 with regression tests and a narrow two-case A3B reporting probe;
see the audit follow-up for exact scope and limits. A07, the trusted finite
review-to-repair coordinator, remains open. n8n was restored after Docker
Desktop had stopped. Do not restart the broad generic Brook schedule. Two
focused documentation investigations and one docstring repair then ran through
local n8n. See the [work ledger](brook-work-queue-2026-10-16.md) for outcomes.

## Goal and scope

Finish a **Brook-only** hardening pass focused on bugs and documentation drift.
Use the local LoopForge + n8n harness, Ollama `qwen3.6:35b-a3b`, and sequential
execution: one repository and one model worker at a time. HelicOps-Cloud and its
`LAUNCH.md` work remain parked. Do not turn this into feature development.

The user wants an autonomous, sustained pass rather than another short generic
review run. Make ordinary local accept/reject choices from source, meaningful
checks and fresh patch-bound review. Do not ask for routine approval already
delegated. Do not merge, push or deploy the product.

## Verified campaign state

At the last check, Brook `campaign_562dedf724f0` was at stage 1, cursor 210,
status `checkpoint`, `runnable=false`, with no slot or worker. Its configured
three-round review schedule is finished. HelicOps `campaign_5b6cbedfbb55`
was parked at cursor 6. Native n8n reported zero running and waiting executions.
The accepted Brook baseline is now
`5dec493ca78e33db0cad84005c86ef5532d03b30`; it includes the accepted
calendar Unicode regression repair, an earlier smoke-probe comment change,
and the 2026-10-16 risk-score docstring correction. That last repair was a
focused documentation task, not a fix for the upload, ICS or logout bugs.

The configured review schedule completed 210 units total; 104 completed after
the qualified harness restart. Of those 104, 75 reports retained unresolved
questions, often because implementation or test excerpts were missing or
mismatched. Three suspected findings were independently rejected as product
bugs. Completion of that schedule is **not** evidence of comprehensive bug or
documentation coverage.

The upload cleanup/duplicate-insert bug, ICS partial-success bug, and logout
rejection/pending-state bug remain unfixed in the accepted baseline. The
original upload, ICS and logout-related coding/investigation attempts were
rejected or exhausted. Preserve those outcomes and all attempt, token and time
counters. Do not reopen, reset or clone an exhausted task to bypass its limit.
A new separately authorized implementation effort, if chosen, must keep its
authorship and evidence distinct from those old tasks.

Focused issue `issue_677ab9b85462` was rejected at its single-attempt limit:
the report omitted the required uncertainty about README “live today” and
“~145K” claims. The narrower observed route/RPC source links were consistent.
Issue `issue_ad91c87177e8` confirmed the stale risk-score docstring; coding
task `coding_3365e68251ad` passed its baseline-failing fixed check and fresh
patch review and was accepted locally. The prior rate-limit source assessment
`issue_25bd711b091c` remains closed; it did not execute a product test.

## Harness readiness and limitations

The local campaign API is at `http://localhost:8124`; the Automation Hub is at
`http://localhost:8050`; n8n is at `http://localhost:5678`. The Hub launcher
reconciles launch intent and activates the production parent/child workflows
when a campaign is actually runnable. At the last check, production workflows
`P94O1VzwXurjkHNe` and `lfCampaignTask01` were intentionally inactive; both
had `saveDataSuccessExecution=all` installed. Never treat inactive workflows,
an Ollama-loaded model or stale Hub archive labels alone as proof of a stalled
or active campaign. Check native n8n **running and waiting** queries, the
LoopForge slot, worker, checkpoint, ledger and retained output.

Substantial harness repairs are installed: strict source/report admission,
bounded pagination, structured review memory, retry accounting, single model
lease, coding checks, atomic finish/recovery, compact n8n continuation, Hub
visibility, authenticated launch reconciliation, and successful-run output
retention. The latest isolated scripted transport check completed 14 units,
with 16 terminal-success n8n executions; all 16 full outputs were downloaded
through the Hub. That proves transport/output retention, not model quality or
product correctness. Earlier real model qualification covered a bounded Brook
repair with immutable checks and fresh review. Keep the baseline coding profile
and qualified `reporter-bounded-v2`; experimental coding/MLX/cache/draft
profiles were not promoted. Do not use Hermes or switch to the 27B model.

Important remaining quality gap: generic source selection can still miss a
complete method and its matching test, or omit an enclosing shell consumer.
It can also turn missing test coverage or action-specific wording into a
spurious bug hypothesis. A correct file or class anchor alone is not complete
contract evidence. Earlier review packets repeated some incomplete claims
across lanes. Require actual expected-versus-observed source, source-complete
test/producer/consumer intervals, and a real failure check before a repair is
accepted. Keep absent evidence as unresolved rather than calling it a defect
or a clean bill of health.

The workspace contains extensive pre-existing and campaign-era uncommitted
LoopForge work. Inspect `git status`; do not reset or overwrite it. The accepted
Brook candidate is in an isolated campaign checkout, not merged or pushed into
the user's main repository. Preserve raw Git patch bytes and the documented
raw-patch backup if restoring candidates.

## Suggested end-to-end Brook pass

1. Reconcile live state and immutable limits. Read this file, the current
   [monitor handoff](/Users/rubensanchez/ai-platform/infra/campaign-monitor.md),
   `.loopforge/campaign/monitor/latest.json`, Brook's campaign snapshot,
   activity/ledger, coding tasks/issues, native n8n running/waiting status,
   and installed workflow/model identity. Save a before snapshot. The monitor
   file has historical sections; its newest section takes precedence.
2. Define a finite, prioritized **new** Brook work queue from actual evidence.
   Start with current accepted-source claims and user-facing documentation.
   For each claim, pair the exact current document text with the implementation,
   consumer, and relevant test. A useful first example is README lines 19–24
   about Water Rights Explorer: the prior generic packet repeatedly read a
   smoke-test header and unrelated data dictionary, missing the actual
   `/research/rights` route and latest `list_water_rights` migration. Verify
   source consistency and distinguish a source claim from unverified live
   database size/deployment claims. Do not invent drift.
3. Fix the remaining automatic evidence-pairing/semantic false-positive cases
   before scaling model review. Add meaningful allowed/denied tests, qualify
   with frozen development and fresh held-out cases, and use a real bounded
   n8n/LoopForge canary. Never promote a profile or claim quality gains from
   test pass counts alone. Keep code-owned sandbox, authority, budget and
   trust boundaries unchanged.
4. Execute bounded Brook-only documentation and bug rounds with local n8n
   loops. Use focused investigations at the existing stage-1 checkpoint for
   source questions; queued focused issues are runnable there. Close or reject
   them promptly after independent source assessment. Do not restart the
   finished generic 210-unit schedule just to increase the cursor. Sequence
   one model worker, one repository, and one focused work item at a time.
5. For verified repair candidates, use separately authorized scope that does
   not mutate or disguise old exhausted contracts. Run the frozen checks plus
   meaningful regressions and negative controls against the accepted baseline,
   perform fresh patch-bound review, check patch identity, then decide local
   acceptance. Record any assistant-authored edits honestly. A closed
   investigation, a passing generic review or a model assertion is not an
   accepted repair. Keep unfixed defects visibly open until that gate passes.
6. Finish with a source-backed documentation-drift ledger: verified current
   claims, corrected claims, unresolved claims and their missing evidence.
   Report product fixes, test results, model/transport results, remaining bugs,
   and exact accepted baseline. Stop only at a clear completion or a genuinely
   scoped blocker. Do not call Brook launch-ready solely because the harness
   works or the broad schedule finished.

The next agent should make this a **one-session autonomous pass** where feasible:
keep working through the queue and resolve routine local decisions. If a
specific action requires authority beyond the user's new instruction, prepare
the concrete patch, tests and evidence first, then ask only for that decision.

## State-changing rules and references

- Use LoopForge domain/operator APIs and authenticated Hub launch; never edit
  campaign SQLite rows to change state. Verify idle before service/workflow
  reload. A Hub launch response marked `existing` is not a new launch receipt;
  reconcile native n8n activity and dispatch intent before retrying.
- Keep one repo/model worker. HelicOps remains parked. Model output never
  expands paths, commands, environment, sandbox capability, approvals, limits
  or source trust. Follow the repo's `AGENTS.md` contribution contract.
- Source of truth and prior limits: `/Users/rubensanchez/ai-platform/infra/campaign-monitor.md`
  and `/Users/rubensanchez/ai-platform/infra/campaign-process-journal.md`.
- Harness implementation evidence:
  [campaign-implementation-results-2026-10-12.md](campaign-implementation-results-2026-10-12.md),
  [campaign-implementation-tracker.md](campaign-implementation-tracker.md),
  [campaign-audit-remediation.md](campaign-audit-remediation.md).
- Latest successful transport evidence:
  `.loopforge/campaign/monitor/qualification/retention-49687d3c` and
  `.loopforge/campaign/monitor/implementation/check-49687d3c-61bb-4c53-a05f-0d4769760361`.
  The qualification receipt contains an old sentence saying it overrode
  retention; loaded settings and the qualification script show it inherited
  `all`, and the raw receipt was kept unchanged.
- Accepted candidate checkout:
  `.loopforge/campaign/coding/coding_3365e68251ad`. Verify the accepted
  commit and clean source before using it; do not assume the main Brook clone
  contains that commit.

Do not paste local API keys, the HomeOS dashboard token, private repository
content or model traces into a public chat or commit. The new chat can read
local files and use existing authenticated local tools.
