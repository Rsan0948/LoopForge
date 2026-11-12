# Recurring repair loop: independent diagnosis

Date: 2026-10-21  
Scope: the stopped `reconciliation-forced-attempt` trial and its frozen harness.

## Conclusion

There are demonstrated harness defects in the feedback and action interface.
This trial does not cleanly measure Qwen's ability to repair the application.
The model sometimes makes a poor initial choice, but the harness then retries
with essentially the same decision problem and fails to communicate its new
rejection. Forced decision recording, stale advice, and restricted investigation
make recovery harder. A model limitation remains possible; it has not been
isolated from these defects.

No application or harness implementation was changed. No local or paid inference
was launched. Existing source, benchmark workspaces, terminal runs, and ledgers
were preserved. New files are this report and local diagnostic artifacts.

## Evidence and reconstruction

Read the escalation prompt, AGENTS.md, current implementation, handoff documents,
intake, frozen manifest, and SQLite events using read-only connections.
Every existing file checked against the frozen manifest matched its recorded
SHA256. The inspected implementation corresponds to this trial.

Reconstructed six inference boundaries using persisted `ContextAssembled` items,
the matching template and tool construction, the `RequiredChecksAwareModel`
wrapper, and the actual Ollama serializer with `httpx.MockTransport`. No HTTP
request left the process. Both prompt messages and complete offered schemas are
saved in `.loopforge/diagnostics/recurring-repair-loop/`.

| Run | Context event sequences | Reconstructed request bytes | Recorded request bytes |
| --- | --- | --- | --- |
| `run_875cdca08db4` | 19, 22, 25 | 25,637 each | 25,637 each |
| `run_282cee9c9b93` | 59, 62, 65 | 33,759 each | 33,759 each |

The historical raw HTTP requests are not separately captured. These are
deterministic reconstructions, corroborated by the frozen code and exact
serialized-size agreement, not independent packet captures. Rejected provider
arguments are also not retained: the guard records the tool name and reason,
not the full rejected decision. Do not infer its wording or the model's intent.

Local artifacts:

- `reconstruct.py`: offline reproduction using mock transport.
- `reconstruction-summary.json`: request sizes and offered tool names.
- `run_875cdca08db4-context-22.prompt.txt`: retry after the first rejection.
- `run_282cee9c9b93-context-62.request.json`: complete forced-phase payload.
- Corresponding `.request.json` and `.prompt.txt` files for all six boundaries.

The source ledger is
`.loopforge/tasks/reconciliation-forced-attempt/goal/worker/runs.sqlite`.
Run the reconstruction from the repository root with
`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python .loopforge/diagnostics/recurring-repair-loop/reconstruct.py`.
It writes only its diagnostic artifacts and does not invoke a real model.

## 1. Confirmed: current-attempt rejection feedback is lost

In the first run:

1. Event 6 proposes a repair decision; event 9 records success.
2. Event 14 reads the PDF parser.
3. Context 19 leads to a repeated decision proposal.
4. Event 20 records `Decision already recorded: do not record it again; apply
   the proposed fix`.
5. Contexts 22 and 25 omit that rejection. The model repeats the proposal and
   the third consecutive failure ends the attempt.

Between these reconstructed requests, only remaining-token and stall counters
change. There is no rejected-action result explaining what happened.

Mechanism:

- `RepairDecisionModel.propose_action` rejects the parsed action **after** the
  Ollama adapter returns successfully.
- The runtime journals `ModelAttemptFailed` and charges usage.
- The reducer leaves model failures out of the working state projection.
- `CodingStateContext` projects tool successes/failures, not model-attempt
  failures.
- Ollama fresh-state correction memory handles its own parse/provider failures;
  it does not receive the outer wrapper's rejection. Successful parsing clears
  that adapter-local correction memory.

References: `campaign_repair_decisions.py:291`, `runtime.py:582`,
`state.py:464`, `campaign_state_context.py:105`, `ollama_model.py:257` and `:473`.

Nuance: later attempts contain the preceding attempt's rejection in
`previous_attempt_evidence.stop_summary`. Therefore it is inaccurate to say the
model never sees any rejection. The missing feedback is the current attempt's
specific rejected action and outcome on its immediate retry.

**Impact:** the retry mechanism does not provide a reason to change behavior.
At temperature zero, nearly unchanged prompts make repeated proposals
unsurprising. That is not evidence that the model knowingly ignored feedback.

## 2. Confirmed: advertised decision schema disagrees with the guard

After a decision exists, `record_repair_decision` remains offered with statuses
`investigate`, `edit`, `verify`, and `unable`. Its description still says to
record a repair decision before work. The wrapper rejects every additional
decision unless its status is `unable`.

In the forced `act` phase, the actual offered tools were:

```
record_repair_decision
apply_edits
write_file
edit_file
format_python_imports
```

The decision schema still advertises all four statuses. Tool-name filtering is
working; argument-level affordances are inconsistent with what the guard allows.

There is also a specific prompt construction defect. `RepairDecisionContext`
first creates phase-sensitive text, including “The repair decision is already
recorded below.” But forced mode unconditionally replaces it with text beginning
“Record best_guess,” regardless of phase. That instruction later says not to
reword a decision. The clear phase-specific instruction has been lost.

References: `campaign_repair_decisions.py:65`, `:193`, `:201`, `:262`, `:314`.

**Impact:** a natural-looking advertised action is repeatedly refused by a
condition that is not reflected in its schema. The fresh request also repeatedly
prompts decision recording at the moment the runtime wants an edit.

## 3. Confirmed: completed local work is poorly distinguished from stale advice

Second run, `run_282cee9c9b93`:

| Event | Observation |
| --- | --- |
| 25 | Proposes the exact PDF row-count edit, six columns to five |
| 28 | Tool confirms successful edit |
| 29 | Fixed verification runs; project failures have fallen from six to three |
| 32 | New decision required; old specialist diagnosis remains in context |
| 35 | Model records a new plan containing the already-applied PDF fix |
| 46 | Source read explicitly shows `if len(parts) != 5:` |
| 49 | Attempt to record another decision rejected |
| 54–57 | Server source read succeeds |
| 60, 63, 66 | Further decision proposals rejected; attempt stops |

The specialist's diagnosis/next steps still describe the old PDF guard and tell
the worker to change it. They are marked model inference and include their old
patch basis, which is good provenance, but no explicit status says that this
local edit has since been applied.

The deterministic specialist receipt covers only the earlier storage edit that
matched the specialist's structured patch. It does not cover the local PDF edit
suggested in prose. Recent actions retain an edit's tool/path/outcome, but not
its old/new replacement. After verification, the latest observation is a short
edit acknowledgement; old file excerpts are correctly invalidated by hash.
The changed-file list mostly describes the whole application, not the recent
repair delta.

At context 32 there is thus no exact post-edit PDF excerpt or explicit completed
PDF edit summary, while the stale specialist instruction is still detailed.
At context 59 the corrected source *is* present, but the newly recorded stale
decision is now locked against revision.

References: `campaign_repair_decisions.py:107`, `:140`,
`campaign_state_context.py:105`, `campaign_repair_escalation.py:200`.

**Impact:** evidence freshness is encoded in hashes, but not presented as a clear
repair trajectory. Choosing the stale repair is observed model behavior;
stale-advice salience as its cause is a supported hypothesis, not proven intent.

## 4. Confirmed: a mistaken plan becomes a behavioral constraint

The recorded decision is correctly classified as model inference, not runtime
authority. Nevertheless, the guard prevents revising it after learning that the
hypothesis is wrong, unless the model gives up with `unable` or changes source
to begin a new cycle.

That couples “do not reset investigation allowance” to “do not update the plan.”
They need not be the same rule. A worker should be able to reject a hypothesis
without receiving a new budget or changing its permissions.

Two investigation actions are also not necessarily two pieces of sufficient
evidence. A source page is capped at 3,000 characters, so one file can require
several reads. In these final second-run contexts the server excerpt did include
the jobs endpoint, so this limit did not make *every* edit impossible. It remains
a general design trap: uncertainty can be converted into forced writes or an
inability stop before enough source has been inspected.

Do not solve this by adding another compulsory planning action. Either make
decision notes advisory and revisable within unchanged limits, or remove the
mandatory decision gate in a controlled comparison.

## 5. Confirmed: escalation does not catch this failure class in time

The ordinary model-failure limit is three consecutive failures. Repair stall
escalation starts at six stagnant context turns; the no-progress stop is eight.
New source reads reset stagnation. These runs therefore reach the three-failure
stop before the specialist sees a sufficient stall signal.

The wrapper classifies decision protocol violations as transient model failures,
so the normal response is retry/backoff. Cross-run recovery specifically looks
for `CODING_NO_NEW_EVIDENCE`, not this `REPAIR_DECISION_REQUIRED` outcome.

References: `domain/reliability.py:20`, `domain/repair_escalation.py:24`,
`campaign_repair_escalation.py:274`, `:338`, `runtime.py:631`.

Do not increase retry limits as the initial fix. More calls with the same faulty
interface would consume additional time. Restore useful feedback first; then
classify protocol dead ends explicitly for bounded recovery if still necessary.

## Decision cycles and wrapper ordering

The cycle calculation itself behaves as implemented in the inspected trajectory:

- Before the local PDF edit: cycle prefix `cbeed3d57c78`.
- After the edit: cycle prefix `e74463f099e4`.
- Reads do not change that cycle; two counted non-edit actions reach `act`.

Projection is run-scoped. A successor run can require another decision even with
the same candidate/advice cycle. The third run starts with the post-edit cycle
and does not inherit the second run's active decision. This is a continuity
limitation across successor attempts, not a failure to persist within a run.

Model ordering is:

```
EscalatingRepairModel
  -> RepairDecisionModel
    -> RequiredChecksAwareModel
      -> DeadlineAwareModel
        -> OllamaModel
```

Context ordering is:

```
StateContextBuilder -> CodingStateContext -> RepairContext -> RepairDecisionContext
```

The checks wrapper appends coding-contract and progress items after the runtime
journals `ContextAssembled`. Those additions were included in the reconstruction.
The event alone is not the complete final provider prompt. Future diagnostics
should capture a redacted final request artifact/hash at the provider boundary.

The outer escalation wrapper can propose an automatic reproduction or specialist
action before invoking the decision wrapper. Ordinary tool authorization still
applies. This makes decision gating a local-worker protocol, not a universal
statement that every action in the run follows that gate.

## What is not established

- No evidence of context-window exhaustion at the reconstructed failures:
  requests are 25,637/33,759 serialized bytes, with a 65,536 context setting and
  4,096 output reserve. Provider input counts in the second run's final attempts
  were 9,611 tokens, with 310–325 output tokens. This is not output-cap exhaustion.
- No evidence that the successful edit was dropped by the event store.
- No demonstrated decision-cycle hash bug in this trajectory.
- No evidence that MLX would correct these protocol failures. Serving speed is
  a separate issue.
- No controlled evidence yet that Qwen cannot solve the remaining repairs with
  a coherent interface, or that fixing this interface guarantees completion.

Qwen runs here with thinking disabled, temperature zero, Q4 quantization, and
fresh state per action. The official model card describes default thinking and
different benchmark harness/settings. Those results are not a guarantee for this
configuration. Evaluate reasoning settings separately after correcting the
demonstrated protocol defects; do not change everything simultaneously.
Source: https://huggingface.co/Qwen/Qwen3.8-27B

## Why passing framework tests did not catch it

Re-ran the nine focused decision/forced-attempt tests: **9 passed**.
The full suite was not rerun; the escalation prompt reports 3,108 passed and 67
skipped for its prior run.

`test_forced_best_guess_requires_edit_and_preserves_runtime_checks` supplies a
scripted sequence containing a bad action followed by a valid edit. The scripted
model supplies that edit regardless of whether rejection feedback was present.
The test also explicitly expects the generic decision tool to remain advertised
in forced mode. These tests prove enforcement, not that the next request gives a
real model the information and legal choices needed to recover.

## Prioritized correction and controlled experiment

### First: a small coherent protocol correction

1. Include the latest rejected proposal's tool, code-owned reason, and explicit
   no-execution outcome in the immediate next model context. Persist it across
   reconstruction, clear/supersede it after a successful proposal, and keep any
   model arguments lower trust. Bound repeated failures without replaying an
   expanding transcript. Preserve immutable budgets and authority.
2. Make forced-mode instructions phase-specific. Once a decision exists, say
   it exists and identify the next legal action. Stop saying “Record best_guess”
   unconditionally.
3. Make the advertised schema agree with enforcement: after recording, offer
   only the allowed inability status for that tool, or expose inability through
   an unambiguous separate capability. Do not advertise forbidden status values.

This is the smallest useful first patch. It repairs communication and interface
consistency; it does not add a new planning layer or another retry guard.

### Next: improve continuity without hard-coding the benchmark repair

- Project a compact exact recent-edit delta with revision/hash and subsequent
  verification outcome. Distinguish “edit applied” from “defect verified fixed.”
- Explicitly mark historical specialist guidance with its basis and changes
  since that basis; preserve it as evidence without continually presenting old
  next steps as the current assignment.
- Allow hypothesis revision without resetting investigation allowance, or test
  removal of the compulsory decision step. Never require a token source edit
  merely to unlock replanning.
- Retain useful prior-attempt repair state with explicit lineage if a bounded
  successor is authorized. Do not resurrect terminal runs.

### Offline acceptance before inference

- Capture final serialized requests through MockTransport, including wrappers.
- Assert a wrapper rejection appears in the next request, not only in the log.
- Assert all advertised decision status values are valid for the current phase.
- Assert prompt text changes between decision-required and action phases.
- Assert local edit evidence survives verification and stale-source invalidation.
- Assert two consecutive defects can be repaired without stale-plan lock-in.
- Test success and denial paths for each modified control, including authority,
  budget, and no-execution semantics.

### Live experiment, not executed during this diagnosis

Create isolated successors from the same preserved pre-repair checkpoint. Keep
model digest, backend, quantization, tool authority, checks, budgets, source, and
sampling unchanged initially. Freeze each configuration; do not mutate a live
or terminal run's policy.

| Arm | Change | Question |
| --- | --- | --- |
| A | Existing harness; saved trials are the initial baseline | What is the observed failure pattern? |
| B | Immediate rejection feedback + phase-consistent prompt/schema | Does the model recover and edit with the same settings? |
| C | B plus advisory/revisable decision notes instead of a mandatory gate | Is compulsory decision choreography itself harmful? |
| D, only if needed | Best harness arm with separately budgeted reasoning enabled | Is inference configuration limiting remaining performance? |

Use repeated trials, not a single lucky edit. Fresh local trials need no new
Gemini call to compare the local protocol; preserve the same historical advice
for the initial boundary comparison. Any later paid escalation experiment should
have an explicit small cap and record its effect separately.

Measure valid edits, rejection counts, new evidence, wall time, input/output
tokens, unchanged checks, verification outcomes, and manual interventions.

Success requires a source edit addressing a pending defect, fixed verification
demonstrating improvement, and continued work on the next remaining defect
without rediscovering the completed repair. Full completion requires all project
checks, trusted acceptance probes, and the existing fresh review. Fewer rejection
messages, an arbitrary write, or an `unable` record is not success.

## Recommendation

Fix the missing feedback and contradictory action interface before changing
models, inference servers, or retry limits. Then test whether the compulsory
decision gate adds value. The evidence supports simplifying this protocol and
preserving a clear repair trajectory; it does not support another forced
planning layer.
