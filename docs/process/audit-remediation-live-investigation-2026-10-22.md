# Investigation: continued looping after audit remediation

Date: 2026-10-22

## Conclusion

The remediation is present, but this continuation does not exercise the full new
repository supervisor. Its local repair protocol still permits a repeat-plan loop
and delays fresh verification. Historical artifacts were preserved without wiring
the previous failure diagnostics into the new worker's initial context.

The local model also demonstrably ignored contrary source evidence. The evidence
does not support blaming only model quality or only the harness. The harness
exposes a predictable failure mode and takes roughly twelve minutes to intervene.

This investigation made no application/harness changes and launched no inference.
The existing background runner continued executing during observation. Its own
paid specialist call is distinguished from this investigation's offline work.

Latest observation before closing this investigation: event 96 records a new
server/test hypothesis following specialist guidance. At 19:02:11 UTC the run
remained nonterminal with no candidate changes. Recovery changed the plan, but
had not yet demonstrated a successful edit or completion. Its new annotation
hypothesis is weaker than the directly reproduced response-validation evidence
in section 6.

## Observed run and status boundary

- Trial: `.loopforge/tasks/reconciliation-forced-attempt/audit-resume-2026-10-22/`
- Worker run: `run_93a0cec7c244`.
- Original candidate identity:
  `fa03aa4af3f11f6e59cfff1f2ec6f7ff6461de812c2976331b52910fa648f945`.
- At event 93, timestamp **2026-10-22 19:00:13 UTC**, no candidate file had changed
  relative to the continuation manifest.
- The run was nonterminal. A fresh context was assembled after specialist recovery.
  This is not a final result or a claim that recovery can never succeed.

The relevant frozen runtime files match the current working tree byte-for-byte:
repair decisions, progress projection, repair escalation, state context, and the
prepared supervisor composition. This is not explained by an old copy of those
modules still running.

## Timeline from actual events and recorded requests

| Events | What happened |
| --- | --- |
| 6 | Worker records a plan to change PDF row length from six to five. |
| 14–17 | Reads `app/pdf_documents.py` from line 90. The returned source explicitly contains `if len(parts) != 5:`. |
| 22–25 | Reads `app/storage.py`; the earlier `check_same_thread=False` correction is also present. |
| 30, 38, 46, 54, 62, 70 | Records the same PDF repair decision again. |
| 75 | Progress projection reaches six stagnant turns. |
| 78–82 | Code-owned escalation forces `check_project`; project tests still show three failures and ten passes. Verification also records remaining acceptance failures. |
| 85–93 | Existing runner obtains new specialist advice and assembles a fresh context. New advice acknowledges the PDF correction and identifies API/test problems. |

The first nine local model proposals contain **seven repair decisions and two
reads**. All seven decision argument objects are identical after canonical JSON
serialization. There are zero edits among those proposals.

## 1. “Revisable” currently includes unlimited identical decisions within the run bounds

`RepairDecisionModel` and `RepairDecisionTools` reject repeated non-`unable`
decisions only when `revisable` is false. With the new flag true, neither checks
whether the decision changed or whether new evidence supports a revision.

The tool description says to revise only when evidence changes the hypothesis,
but the executable boundary accepts the identical object repeatedly. Each response
is recorded as `ToolSucceeded`, so the worker receives an apparent successful
action result for doing no new work.

The overall loop is bounded by other controls; it is not mathematically infinite.
Nevertheless, accepting a repeated plan seven times is sufficient to recreate the
user-visible failure.

Sources:

- `src/loopforge/entrypoints/campaign_repair_decisions.py`,
  `RepairDecisionModel.propose_action`, `RepairDecisionTools.execute`.
- Trial `worker/runs.sqlite`, events listed above.

**Needed correction:** distinguish a real revised hypothesis from an identical
acknowledgement. Give immediate no-progress feedback and a bounded recovery path.
Do not solve this by restoring the old rule that prohibits useful revisions.

## 2. The two-read forced-attempt rule removes the useful escape routes

The continuation freezes:

```json
{
  "repair_decisions": true,
  "force_repair_attempt": true,
  "revisable_repair_decisions": true,
  "review_provider": "gemini"
}
```

After two reads, `filter_repair_tools` advertises only:

```text
record_repair_decision
apply_edits
write_file
edit_file
format_python_imports
```

This is visible in the fourth through ninth saved worker requests. The worker
cannot choose `check_project` or inspect the actual failing API code through its
advertised tools at this point. The guard independently enforces the restriction.

Meanwhile, the state packet says the workflow is in `verify` and, when no current
failure is recorded, instructs the worker to check the current candidate first.
The outer repair policy says to edit before further checks. These are conflicting
operational directions in the actual same request.

The model should have recognized the already-applied PDF repair and selected a
different course before exhausting its reads. The harness should still provide a
valid way to recover from that mistake without an arbitrary edit or an inability
declaration.

Source: `src/loopforge/entrypoints/campaign_repair_decisions.py`,
`RepairDecisionState`, `RepairDecisionContext`, `filter_repair_tools`.

**Needed correction:** use one coherent phase contract. Fresh verification and
targeted investigation must remain available when the prior hypothesis is disproven
or current diagnostics are absent, within frozen execution budgets.

## 3. Preserved history is not being delivered as usable continuation evidence

The preparation script copies historical run events into `worker/runs.sqlite` and
copies the specialist ledger. It also runs preflight checks showing the current
three failures. However, the new worker's recorded requests initially contain:

```json
{
  "previous_attempt_evidence": {"records": []},
  "current_verification": {"summary": "No pending verification failure."}
}
```

Worker feedback is assembled from `coding_events` for the new task. Copying old
runtime events does not populate that task's failed/retry event details. The
preflight report is a separate file and is not automatically injected as a
candidate-bound verification observation.

The old specialist diagnosis, by contrast, is included in full on every request.
It is honestly labeled historical, but still provides detailed, now-obsolete
instructions about the PDF and SQLite defects. The initial evidence balance is
therefore poor: old actionable advice is prominent; fresh failing-check evidence
is absent. `recent_successful_edits` is also empty for the new run, despite the
preserved candidate containing previous edits.

Sources:

- Trial `prepare.py`, historical-event import and specialist-ledger copy.
- `src/loopforge/entrypoints/campaign_execution.py:341`, task-local feedback query.
- Trial `worker-prompts/*.json`, original serialized requests.

**Needed correction:** construct an explicit continuation evidence bundle tied to
the seeded candidate. Include prior successful edits and fresh failure artifacts
as observations, preserving their provenance. Archive superseded advice rather
than repeatedly presenting its full next-step instructions as active guidance.

## 4. This trial uses the prepared supervisor, not the new investigative supervisor

The launch script instantiates `PreparedGoalService`. Persisted policy shows:

```json
{
  "investigation_enabled": false,
  "require_recovery_evidence": false,
  "investigation_contract": "",
  "assignment": null
}
```

Consequently, the new repository investigation, candidate-inspection workflow,
assignment revision, and evidence-required retry policy are not enabled here.
The new frontier reviewer is configured, but review happens after the coding stage
reaches review readiness; it cannot rescue these initial coding turns.

There is a second composition difference: `RepositoryGoalWorker.prepared` forwards
the supervisor's latest plan/rationale through `assignment_guidance`.
`PreparedGoalService.worker` directly constructs `CampaignGoalWorker` without that
bridge. The captured local requests contain no `latest_supervisor_plan_untrusted`
field. Updating the prepared supervisor's plan is therefore not the same as
delivering revised instructions to its coding worker.

**Needed correction:** qualify the intended repository supervision composition on
an explicitly prepared successor, or give prepared goals equivalent bounded
observation and guidance capabilities. Do not claim that implementing a feature
elsewhere means this particular trial uses it. Do not mutate the live frozen
policy to turn those capabilities on.

Sources: trial `run.py`, `latest-state.json`;
`src/loopforge/entrypoints/supervisor.py:180`, `:215`;
`src/loopforge/entrypoints/repository_supervisor.py`, `RepositoryGoalWorker.prepared`.

## 5. Recovery exists, but its latency is too high for this failure

`stagnant_turns` correctly identifies the repeated no-progress state. Fresh source
reads reset the counter. Specialist escalation starts at six stagnant contexts;
the local stopping boundary is eight. At approximately 80–100 seconds per local
request, several useless decisions become many minutes.

Read-only decision tools skip ordinary verification. The frontier goal supervisor
also reconciles active coding work without issuing a new supervisory decision.
The specialist wrapper is therefore the first automatic intervention in this run.

During this investigation, it did activate: forced reproduction at event 78, then
new specialist advice. A claim that escalation never runs would be false. The
problem is delayed, coarse intervention coupled with repeated accepted no-ops.

**Needed correction:** detect identical decision repetition immediately; distinguish
useful investigation from repeated acknowledgement; make active-worker progress
visible to supervision. Use elapsed-time awareness as well as turn counts while
preserving hard budgets and single-owner execution.

Sources: `src/loopforge/entrypoints/campaign_progress.py`;
`src/loopforge/domain/repair_escalation.py`;
`src/loopforge/entrypoints/campaign_repair_escalation.py:292`;
`src/loopforge/application/supervisor.py:178`.

## 6. The remaining app failures are concrete, and the app hides their cause

Read-only source inspection plus an isolated diagnostic in the trial's pinned
container reproduced two response-construction failures:

1. `Storage.list_jobs()` uses `SELECT *`, returning `created_at`. `JobOut` inherits
   `extra="forbid"` and has no `created_at` field. `JobOut(**row)` raises
   `ValidationError(extra_forbidden, created_at)`.
2. The upload route adds `job_id` to the internal document dictionary, persists
   it, then calls `DocumentOut(**doc)`. `DocumentOut` forbids `job_id`, so response
   construction fails after the database write. Stored document rows additionally
   include internal fields such as `imported_at` that response projection must
   handle deliberately.

The blanket Pydantic `ValidationError` handler maps these internal errors to HTTP
422 `{"error":"invalid request"}`. That conceals the distinction between bad
incoming requests and bugs in the server's response construction. The diagnostic
also confirmed that the document exists in storage before response validation
fails.

The calculation test has a separate fixture problem: `_row` imports a whole new
document each time it adds a row. Calling it repeatedly for the same job/kind
violates the document table's unique constraint. The recorded test output shows
that exact failure.

The fresh specialist correctly identified the repeated PDF hypothesis as obsolete,
but suggested route annotations as one possible cause of the 422s. The direct
probe provides stronger evidence for explicit storage-to-response projection.
This is why specialist suggestions must be tested rather than treated as commands.

Diagnostic output:

```text
JOB_OUT: extra_forbidden at created_at
DOCUMENT_OUT: extra_forbidden at job_id
DOCUMENT_PERSISTED_BEFORE_RESPONSE_VALIDATION: True
```

The container mounted the candidate read-only, used no network, and wrote only
temporary diagnostic SQLite data. No candidate file or production database was
modified. The run's real checks remain authoritative for final acceptance.

## Recommended next implementation and verification

1. Fix the continuation evidence handoff before another long trial.
2. Reject identical repair-plan no-ops with useful feedback; allow genuine
   hypothesis revision.
3. Remove the contradictory forced-edit bottleneck when the current failure is
   unknown or the hypothesis was disproven. Maintain code-owned tool authority.
4. Ensure the selected supervisor composition actually delivers current-candidate
   inspection, revised worker guidance, and meaningful retry controls.
5. Add a deterministic regression using this exact sequence: stale plan → source
   disproves it → same plan repeated → correction → fresh check → new hypothesis.
   Assert serialized request content and available tools, not only final state.
6. Run a bounded live qualification on a new frozen successor after those tests.
   Record time to first useful check/edit, repeated-plan count, actual diagnostics
   delivered, and independently verified completion.

Do not increase the iteration limit as the primary fix. This trial already permits
192 iterations and four hours per worker configuration. The missing property is
useful recovery early enough to avoid wasting that allowance.
