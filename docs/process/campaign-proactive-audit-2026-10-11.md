# Proactive campaign audit — 2026-10-11

Scope: local LoopForge campaign composition, native Ollama controller/reporter, coding and patch review, live n8n definitions, activity projections and the external campaign monitor. This is a targeted source and live-evidence audit, not a claim of exhaustive security or repository correctness. Brook only; HelicOps stays parked. One model agent at a time; no subagents. No budgets, runtime authority, exhausted tasks or accepted baseline were changed.

## Outcome and immediate repair

The previously blocked bounded review `run_346becf83752` succeeded in 41.25 seconds with zero invalid responses. The next generic challenge review `run_8d7c32d501b3` also succeeded, in 127.54 seconds and 86,369 recorded tokens. Reconciliation `run_f1f99a97b229` then failed in 6.08 seconds before any action: three HTTP 500 responses, unknown usage on all three.

Ollama's corresponding log identifies malformed native tool-call XML, at approximately 45.6 generated tokens/second. A diagnostic replay of the initial model request, with action execution disabled and all campaign workers idle, reproduced it. The strict JSON error body contains only `XML syntax error on line 3: element <function> closed by </parameter>`. It lacks both keywords required by the earlier classifier. This is a confirmed adapter compatibility gap, not evidence of slow inference.

Fixed `adapters/ollama_model.py:469`: bounded HTTP 500 JSON errors matching that exact function/parameter mismatch now get `MODEL_TOOL_PARSE` and the existing fixed protocol correction. Generic XML errors, arbitrary suffixes, malformed/oversized bodies and other statuses retain their prior classification. Provider text is never placed into the next model request or durable error summary. No partial tool call is executed. Tests cover both recovery and denial, unchanged tools/options, usage uncertainty and per-run separation.

Validation: 68 adapter tests passed; full suite **2,715 passed, 50 skipped in 48.73 seconds**; configured Pyright, changed-file Ruff/format and diff checks passed. Workers/slots and n8n running/waiting executions were all idle before reload. Resume/Hub dispatch occurred once at cursor 20. New `run_9d7708d426c6` actually exercised `MODEL_TOOL_PARSE` once, then successfully proposed and executed subsequent read-only tool actions. This proves live recovery of this error shape. The run subsequently completed in 130.39 seconds with 82,529 recorded tokens, one invalid/unknown-usage attempt; n8n advanced to cursor 21 and began run_0ab3c0be447b. Its report still repeated unsupported hypotheses, so process recovery is not substantive correctness.

## Remaining surfaces, ordered by impact

### 1. Successful responses can hide missing usage — high, reproduced

Both `adapters/ollama_model.py:77` and `adapters/ollama_reporting.py:19` default absent `prompt_eval_count` / `eval_count` to zero. Schema-valid successful envelopes without counters therefore become known zero usage. A hermetic reproduction confirms this for both adapters. Error responses with unavailable usage are tracked separately, but this successful-response path does not signal uncertainty.

Impact: recorded token totals can understate consumption, and token ceilings cannot be described as an exact physical bound in this case. Current retry, iteration and elapsed controls still exist; this is not proof of unlimited execution or a presently missing Ollama counter.

Next: require usage fields for completed provider responses, or introduce an explicit unknown-usage policy that conservatively stops/charges against a code-owned reservation. Never fabricate zero consumption. Tests must cover absent, partial, malformed and explicit-zero counters, including a valid action that must not execute when consumption cannot be accounted for. Keep actual local inference consumption distinct from estimated reservations.

### 2. Recent reports lack revision and persistent disposition — high, reproduced

`entrypoints/campaign.py:174` selects recent reports and one resume message; its output omits the stored revision and has no current-revision filter. A synthetic old-revision report is accepted into context and loses its revision attribution. Only the last few reports survive rotation. Journal assessments are not automatically injected into the model.

Impact: rejected `/rights` retirement, Phase 6 and retry-count hypotheses recur; fresh reviews can spend their allowance proving already settled claims. A prior report is correctly lower-trust evidence, but that alone does not preserve useful memory.

Next: append-only, revision-aware finding records with stable identity, scoped evidence, operator disposition and next check. Include the pinned revision in every prior report; label older evidence historical. A rejected hypothesis may reopen only on concrete new evidence or changed relevant source. Rejection of an investigation must remain distinct from repair of its defect. Tests should cover revision changes, duplicate hypotheses, superseded evidence, omitted old records and rejection that cannot expand authority. Do not automatically relabel existing reports or clear exhausted repairs.

### 3. Generic workload coverage remains too weak — high, observed

`ReviewTools.verify` at `entrypoints/campaign_review.py:502` enforces mandatory intervals when configured. Generic units have no such contract; any actual excerpt plus a structurally valid report can pass. The generic challenge and reconciliation read plan/handoff/README pages despite prompts asking for new evidence. Challenge read plan lines 40–64 repeatedly, then called its table contradictory; the explicit historical/superseded notice is immediately outside that page at lines 67–72. Independent pinned-Git reading rejects this alleged contradiction. Reconciliation alleged a missing versioned prototype without checking its path; pinned Git contains it and its SHA-256 matches the canonical expected `0dda713bcacbff4a0cb15da40b48e633d4987c187213924aff8392074c7aa5f5`. Both reports also repeat the unsupported architecture-retirement claim. This is direct evidence that page boundaries, negative claims from absent packet content and poor disposition memory are affecting quality. The generic cap discloses sampling but does not make these reviews producer/consumer or behavior checks.

Next: replace broad repeated scope/angle prompts with a bounded queue of precise hypotheses and code-owned evidence obligations: relevant implementation branch, consumer, failure/denial test and governing document where needed. Give documentation-only tasks documentation contracts; do not invent implementation coverage for them. Stop a task as inconclusive when its required evidence cannot fit. Track distinct evidence coverage and repeated reads, not merely cursor advancement. For documentation contradictions, inspect section dates, supersession notices and immediately adjacent continuation before calling historical text current; an evidence packet is not a complete file inventory. Preserve enough discovery work to find unknown bugs; do not replace discovery with a list of already known claims.

### 4. Valid citations do not prove their associated claim — high, observed

The successful bounded report calls consistent layout a low/uncertain finding, overstates what AGENTS says about root routing, and cites root lines before the actual redirect. Independent reading of the complete 11-line root file supports routing behavior, but the selected quotation alone does not. `referenced_report` and quotation verification establish source identity and exact text, not entailment.

Next: separate descriptive observations, suspected defects, rejected hypotheses and actual blockers in the report. Require explicit claimed behavior, observed contrary behavior and a task-relevant next check for defect candidates. Preserve operator assessment rather than promoting model agreement to correctness. Evaluate evidence adequacy with known false-positive fixtures; deterministic checks can enforce presence/identity but cannot reliably decide arbitrary semantic truth. Avoid adding a second unconditional model reviewer to every unit.

### 5. Parent n8n lifetime does not match campaign lifetime — high, configured risk

Both checked live workflows and `integrations/n8n/campaign-driver.json` set `executionTimeout: 10800`. The parent loops over bounded units and waits for each child. At the checked Brook configuration there are 210 units; their aggregate duration can exceed three hours even when each unit respects its own limits. Official n8n documentation confirms that workflow timeout cancels the current execution. No parent timeout event was reproduced in this audit.

Next: use bounded orchestration batches with durable campaign checkpoints, then a periodic dispatcher that reconnects or launches only when runnable and no current parent/child/slot exists. Keep the idempotent child claim. Test parent expiry while a child is active and while the harness is ready. Never release an occupied slot or duplicate a model worker because a parent timed out. This is preferable to simply removing the parent timeout.

### 6. A queued check-in is not guaranteed active supervision — high, observed

`~/ai-platform/infra/campaign_monitor.py:50` coalesces forever while pending; an ambiguous queue timeout also sets pending indefinitely. The recorded pending check persisted from 06:28Z until the later continuation/ack. Heartbeats continued, but no completed assistant check was established by those heartbeat entries. A late ack has no generation/check ID, so it can also clear a newer pending message.

Next: separate queued, started, heartbeat and completed check identities; report overdue supervision visibly. Reconcile a pending receipt against actual queue/thread execution state before any bounded redispatch. Do not blindly expire pending and create duplicate agents. Collect n8n running/waiting, slot ownership, last model/tool progress and current provider request status as distinct signals. Test ambiguous submission, overdue-but-running checks and a stale ack that cannot clear a newer check. The timer cannot honestly promise an assistant will be active “no matter what.”

### 7. Cached snapshot provenance is trusted after creation — medium, source-confirmed

`CampaignHarness.create` writes a pinned-Git snapshot. `drive` at `entrypoints/campaign.py:602` reads the mutable JSON cache when the original revision is used, without validating it against blob identities or a trusted digest. Original creation does validate Git blob identity. Later accepted-baseline reviews reconstruct pinned files, so this cache branch does not describe Brook's present accepted revision.

Next: store a manifest of revision/path/blob identity and verify cached content, or regenerate from Git. Fail closed on corrupt or mismatched cache; never label altered cache content as the original commit. Add a cache-tampering regression without mutating the live snapshot.

### 8. Repeated campaign-level resumes have no aggregate review allowance — medium, source-confirmed

Review run limits reset for every new run at the same cursor; `resume` does not apply a per-unit aggregate retry/token/time contract. Coding and issue tasks do have aggregate caps and must retain them. This audit used a justified new run after a concrete adapter fix; it does not authorize endless identical retries.

Next: durable per-workload attempt accounting and failure fingerprints, with bounded recovery eligibility after a documented implementation/environment change. Introduce new controls before new workload execution; do not retrofit larger budgets into active contracts. Preserve failed runs and refuse unchanged deterministic retry loops. Test continuation after a genuine corrective change and denial when aggregate limits are exhausted.

### 9. Progress API grows with history and can look frozen during inference — medium, source-confirmed

`campaign_visibility.py:62` enumerates every referenced run, replays its full event stream and returns every timeline/artifact on every request. It uses the last persisted event to calculate elapsed time; an active provider call emits no intermediate progress event. `worker_alive` is global and appears on parked repositories too. The standalone monitor reads campaign snapshots and loaded models, not n8n executions or latest run progress.

Next: bounded recent-run summaries and paginated per-run events, an explicit active-slot owner, and clearly labeled wall-clock age versus last durable activity. Expose “waiting for provider” with request start/deadline through a read-only projection. Do not treat a loaded model, worker flag, or no recent event as proof of productive work or a deadlock. Preserve historical evidence via drill-down.

### 10. Coding feedback can lose record boundaries — medium, source-confirmed

`campaign_execution.py:38` joins JSON records and then slices the last 12,000 characters. This can start in the middle of a record and omit the relevant earlier failure. The generic review path already packs complete compact report objects more carefully.

Next: pack whole typed feedback records in priority order, include an explicit omission count, and preserve current patch identity, failing check and latest requested change. Never let diagnostic text modify registered checks or tool authority. Test an oversized single record and many records with the critical current failure at either end.

### 11. New short citations trade ease of generation for expressiveness — medium, design limitation

The reporter permits two findings, two references per finding and 1–3 lines per reference. This avoids the observed whole-page serialization trap, but a nonlocal contract can need more evidence. Source lines longer than 1,000 characters remain uncitable through exact quotations. Neither the line cap nor a schema-valid report establishes comprehensive coverage.

Next: disclose report capacity, preserve overflow claims in a bounded follow-up queue, and consider code-generated exact snippet selectors for unusually long lines. Do not silently truncate source, retarget IDs or increase evidence scope to make a report pass. Qualify a changed protocol against multi-file/nonlocal and long-line fixtures before activation.

## Implementation order and release evidence

1. Immediate parser shape recovery is fixed and exercised live.
2. Address missing usage and revision/disposition memory with hermetic allow/deny tests; activate only at a verified idle boundary.
3. Introduce precise workload/coverage records and semantic quality fixtures. Report success, factual adequacy and accepted repairs separately.
4. Bound n8n parent batches and reconcile supervision delivery without duplicate work.
5. Verify snapshot provenance, pack whole feedback records, paginate activity and add remaining-deadline-aware provider calls. Current synchronous controller/coding HTTP timeouts can delay cancellation until the call returns; reporter timeout already uses remaining run time. Do not claim an interruptible hard wall-clock boundary from a polling loop.

Keep fixed budgets, check definitions, authority labels, sandbox capability requirements, terminal-state rules and one-repository/one-agent scheduling unchanged. Qualify each control using both allowed behavior and failure/denial evidence. No model-only approval may substitute for source/tests and unchanged patch identity.

## Primary references

- [n8n workflow settings](https://docs.n8n.io/build/manage-workflows/configure-workflow-settings.md): execution timeout, error workflow and saved progress behavior. The three-hour setting above was independently read from the live local workflow API.
- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs): schema-constrained generation and validation with Pydantic/Zod. This supports mechanical output constraints, not semantic correctness guarantees.
- [Ollama tool calling](https://docs.ollama.com/capabilities/tool-calling): the application owns execution and returns tool results in the model conversation. Existing native conversation plumbing should be retained.

Artifacts: `.loopforge/campaign/monitor/implementation/proactive-audit/provider-reproduction.json` and `contract-reproductions.json`. Runtime error summaries and correction prompts remain redacted; the diagnostic executes no proposed action.
