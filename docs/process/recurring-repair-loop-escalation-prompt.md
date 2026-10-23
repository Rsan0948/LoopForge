# LoopForge recurring repair loop: independent diagnosis prompt

Please independently diagnose a recurring failure in our autonomous coding harness, LoopForge. Focus on the harness/model interaction, not on fixing the benchmark app.

Repository: `/Users/rubensanchez/Developer/LoopForge`

Read `AGENTS.md` first. Preserve existing dirty/untracked work. Do not launch paid inference or modify the benchmark application during diagnosis.

## Objective

We want a local coding model to finish tasks autonomously, with durable state, strict tool schemas, bounded investigation, verification, and frontier-model escalation after repeated failures.

## Setup

- Local Ollama model: `qwen3.8:27b-q4_K_M`, digest prefix `25b843619e94`.
- Thinking disabled; 4,096 output tokens; 65,536 context window.
- Each request gets a fresh context assembled from current state and evidence, rather than replaying chat or using compaction.
- Gemini orchestration and repair specialist use separate stateless sessions.
- Specialist guidance persists in a SQLite ledger and is projected as model inference.
- Successful edits and verification outcomes persist as domain events.
- Permissions, writable scope, checks, and budgets are code-owned and immutable within a run.

## Benchmark

A local manufacturing invoice reconciliation app using FastAPI, SQLite, vanilla frontend, and structured PDF imports. The model previously built substantial functionality, but gets stuck during focused repair.

## Recurring failure

The model repeatedly reads files, restates plans, or reruns unchanged checks instead of applying a repair. We added increasingly explicit controls, but these often turn the behavior into repeated rejected proposals rather than productive edits.

## Latest controls

1. Before working, call `record_repair_decision` with: `target_defect`, `path`, `missing_evidence`, `next_action`, `best_guess`, `status`.
2. The record persists across fresh contexts as `MODEL_INFERENCE`.
3. In forced-attempt mode, `best_guess` must describe a concrete source change.
4. After two non-edit actions, the advertised tools and an independent proposal guard permit only source edits or an explicit inability record.
5. Re-recording a decision for the same candidate/advice cycle is rejected, except `status unable`.
6. A successful source change starts a new decision cycle.
7. Existing scope, hash, edit, sandbox, and verification controls remain in force.

## Observed latest trial

Directory: `.loopforge/tasks/reconciliation-forced-attempt/`

- First attempt recorded a decision, read the PDF parser, then repeatedly proposed `record_repair_decision` again.
- Second attempt correctly changed the PDF parser’s row-count guard from six columns to five. `ToolSucceeded` confirmed the edit.
- Project tests improved from six failures to three.
- After that edit, it recorded a new decision that still included the already-fixed PDF defect, read source showing the correction, and again repeatedly proposed `record_repair_decision` instead of another edit.
- Third attempt also stalled on repeated decision proposals.
- Supervisor eventually marked the goal blocked; the process is stopped.

Exact rejection:

> Rejected tool record_repair_decision: Decision already recorded: do not record it again; apply the proposed fix. No action executed or allowance refreshed.

Run IDs:

- `run_875cdca08db4`
- `run_282cee9c9b93` — successful PDF edit
- `run_71e5622ffafb`

Earlier trial: `.loopforge/tasks/reconciliation-decisions/`

Had similar failures, but its rejection message was too generic to identify the attempted tool. The newer diagnostic confirms repeated decision proposals.

## Important uncertainty

We do not know whether the main cause is:

- Contradictory or poorly prioritized instructions in assembled fresh contexts.
- Failure to represent completed versus pending repairs clearly.
- Tool schema/catalog design encouraging repeated decision calls.
- Decision-cycle or event-projection bugs.
- Retry feedback not reaching the next inference effectively.
- Context size/salience.
- Model limitations.
- Several of these together.

Thinking was disabled, so we have no private reasoning trace. The recorded best guess is an observable plan, not chain-of-thought. Do not invent an explanation for the model’s intent.

## Relevant implementation

- `src/loopforge/entrypoints/campaign_repair_decisions.py`
- `src/loopforge/entrypoints/campaign_state_context.py`
- `src/loopforge/entrypoints/campaign_repair_escalation.py`
- `src/loopforge/entrypoints/campaign_progress.py`
- `src/loopforge/entrypoints/campaign_coding.py`
- `src/loopforge/entrypoints/campaign_execution.py`
- `src/loopforge/adapters/ollama_model.py`
- `src/loopforge/entrypoints/goal_worker.py`

## Relevant evidence and tests

- `docs/process/repair-decision-handoff.md`
- `tests/unit/test_repair_escalation.py`
- `tests/unit/test_ollama_model.py`
- `.loopforge/tasks/reconciliation-forced-attempt/goal/worker/runs.sqlite`
- `.loopforge/tasks/reconciliation-forced-attempt/intake.json`
- `.loopforge/tasks/reconciliation-forced-attempt/run.log`
- `.loopforge/tasks/reconciliation-forced-attempt/frozen-manifest.json`

The framework suite passed 3,108 tests, with 67 skipped. Scripted tests demonstrate allowed edits and rejection behavior, but clearly do not establish that the real model can navigate this protocol effectively.

## Requested investigation

1. Reconstruct an actual failed inference boundary: the exact assembled prompt, offered tool schemas, persisted decision, current source evidence, and retry feedback. Verify what was actually sent, rather than assuming our intended design reached the model.
2. Audit decision-cycle calculation, event projection, wrapper ordering, and tool filtering for inconsistencies.
3. Identify contradictions, stale instructions, redundant planning requirements, or affordances that encourage re-recording a decision.
4. Distinguish demonstrated defects from model-behavior hypotheses.
5. Recommend the smallest useful fix and a controlled experiment that separates harness causes from model causes.
6. Define measurable success: a valid edit followed by verification and continued repair, not merely fewer guard errors.
7. Avoid adding another planning layer or guard unless the evidence supports it.

Give a candid diagnosis with concrete evidence and a prioritized next step.
