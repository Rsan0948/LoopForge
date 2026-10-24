# Full harness audit — 2026-10-24

Scope: the whole path a goal takes today, from `repository_supervisor launch` to a delivered
candidate. That covers the Gemini supervisor, the goal reducer and coordinator, the campaign
coding service, the worker runtime and its stop rules, the tools, the verifier and probes, the
Gemini reviewer, budgets, and the tests. The evidence is the code at `12967e8` and the recorded
event streams of the three Snake runs and the delivered reconciliation repair.

Standing rule, from the user's request on this date: every fix is tested for its second- and
third-order effects offline before any live or paid run. Each fix below lists the consumers it
touches and the test that proves it.

## 0. Verdict

The loop is not far off, but it fails *by construction* on long runs. The worker and supervisor
together carry about 30 independent limits across three layers:

- **Worker runtime:** no-progress, tool circuits, iterations, tokens, time, and model-failure streaks.
- **Worker wrapper:** no-new-evidence, check-blocked, stalled-retry, and deadline.
- **Goal layer:** decisions, failures, action rejections, research failures, stagnant decisions,
  assignment versions, attempts, review attempts, cost, deadline, and the parent budget.

Each limit is unit-tested alone. No test drives a realistic long run through all of them at once.
A long run is exactly what a from-scratch build is, so each live build has found the next limit.

The code confirms that this will continue. After today's three fixes, the next Snake attempt
would most likely stop on one of these:

- **F2:** the no-progress counter, because probe scores only move when a whole probe flips.
- **F3:** the token budget, at about 45 turns.
- **F1:** a tool circuit opened by the model's own argument mistakes.
- **F5:** a supervisor `retry` after the attempts are used up, which blocks the goal.

None of these needs a live run to find. All four are found below by reading the code and
replaying recorded runs.

Two findings explain most of today's Gemini spend:

- **F4:** after a failed attempt, Gemini sees only *which* checks failed, never *why*. That drove
  19 inspections, including the same `game.js` read 7 times.
- **F10:** each decision reserves about 5× what it is ever charged, so goals stop with money left.

Plan: build the missing test infrastructure first (Phase A), then fix by class (Phases B and C),
then run free local smokes, and only then a paid run (§5).

## 1. How a goal runs today

```
launch -> GoalExecutionService (runner thread) -> GoalCoordinator.step loop
  supervisor decision (Gemini, stateless, <=48 KB request)  -> GoalState reducer
  prepare / reproduce / inspect / retrieve -> RepositoryInvestigation (records + evidence store)
  delegate / retry / verify -> CampaignGoalWorker -> CampaignHarness -> CodingCoordinator.drive
      coding run:  Runtime(RequiredChecksAwareModel(DeadlineAwareModel(OllamaModel/MLX)))
                   tools: ConfinedCodingTools -> sandbox; verifier: CodingVerifier -> RepairVerifier
                   (every successful write re-runs all checks + 3 Chromium probes)
      review run:  GeminiReviewModel over ReviewTools, PatchReviewVerifier (full coverage, no findings)
  deliver -> GoalStopped(delivered) only with a runtime-verified, reviewed candidate
```

## 2. Method

- Read every module on that path:
  - `goals.py`, `investigation.py`, `application/supervisor.py`, `gemini_supervisor.py`,
    `gemini_review.py`
  - `goal_worker.py`, `goal_investigation.py`, `campaign_execution.py`, `campaign_coding.py`,
    the review admission parts of `campaign_review.py`
  - `runtime.py`, `state.py`, `policy.py`, `reliability.py`, `ollama_model.py`, `ollama_profiles.py`
  - the verifier in `workloads/repair.py`, `check_receipts.py`
- Replayed the event streams of `snake-a3b-mlx-2026-10-24{,-r2,-r3}`: every tool call, score,
  failure, circuit and stop, plus per-turn latency and tokens. Replayed all 107 goal events of r3.
- Measured Gemini usage across 198 recorded decisions.
- Confirmed F1 with an offline script; the output is under F1.
- Ran the offline suite: 3,418 passed, 63 skipped.

## 3. Findings

Severity:

- **P0:** will stop or block the next build.
- **P1:** wastes money or time, or misleads a model.
- **P2:** structure and process.

### P0

#### F1. Tool circuits open on the model's own argument mistakes, never close, and the model is not told

- **Mechanism:**
  - `state._update_streak` counts failures per tool name and resets only when *that same tool*
    succeeds. "Three consecutive failures" therefore means three failures of `edit_file` with
    no successful `edit_file` in between, even if many reads and writes happen between them.
  - `ConfinedCodingTools` reports every model mistake as a PERMANENT tool failure: a non-unique
    edit target, a stale `sha256`, bad read arguments, an unchanged edit.
  - At 3, `CircuitOpened` blocks the tool for the rest of the run. There is no half-open state.
  - A blocked proposal produces `ActionRejected(BLOCK_CIRCUIT_OPEN)`. That reaches the model only
    as "No new observation was recorded for the proposed action" (`OllamaModel._no_observation`).
- **Proof:** an offline script ran this sequence through the real `Runtime`: three `edit_file`
  calls with a wrong `old` text, each followed by a successful `read_file`, then one correct edit.

  ```
  ToolFailed TOOL_ARGUMENTS   x3
  CircuitOpened edit_file
  ActionRejected BLOCK_CIRCUIT_OPEN {'old': 'right', ...}   <- the correct edit is refused
  ```

- **Live exposure:**
  - r3 had a `TOOL_ARGUMENTS` edit failure in its first attempt.
  - Every attempt opened with a `CODING_READ_ARGUMENTS` read failure (see F8).
  - `RequiredChecksAwareModel` stops the run only when a *check's* circuit opens. When an edit or
    read circuit opens, the worker silently loses that tool and then dies on no-new-evidence.
- **Fix (class):**
  - Circuits exist for flaky dependencies. Model input errors must not count toward them.
  - Mark tool results that reject the model's input with a distinct non-infrastructure
    classification, for example `error_code` prefixes `CODING_*`, `TOOL_ARGUMENTS` and
    `NAV_ARGUMENTS`, and exclude them from the streak. Keep sandbox errors, timeouts and policy
    denials counting.
  - Turn `BLOCK_*` rejections into a model-visible observation that names the reason.
- **Downstream checks:**
  - The circuit pin tests (`test_reliability_runtime`) must still pass for real infrastructure
    failures.
  - No-new-evidence must still bound a model that repeats a bad edit forever. It does: an unchanged
    patch with no new reads still counts 8 turns.
  - `measured_summary` and `worker_failure` are unaffected. Replay of old streams is unchanged,
    because classification happens at tool time.

#### F2. The no-progress counter cannot see progress inside a probe

- **Mechanism:**
  - `_verification_progress` requires the score to strictly *exceed* the best score so far.
  - The score is passing checks divided by checks. Partial credit exists only for pytest summaries.
    The Snake probes are inline Playwright scripts that stop at the first failed assertion, so each
    probe is worth 0 or 0.2.
  - Fixing gameplay assertion 4 of 20 therefore leaves the score unchanged. Every edit that does not
    flip a whole probe is a strike, and the limit is 8.
- **Live evidence:** r3, run `3d436f35`:
  - The score sat at 0.72 through 5 verifications after edits.
  - The circuit ended that attempt first. Under today's code it would have continued, then stopped
    on this rule at 8.
- **Fix (class):** give inline acceptance probes partial credit from *how far the script got*.
  - The partial credit is the line number of the outermost failing `<trusted-acceptance>` frame,
    divided by the number of probe lines, capped below 1.0.
  - Use the outermost frame: module-level lines are monotone in a linear script, and helper
    functions are not.
  - The probe source stays hidden, and a later failing line still requires passing every earlier
    assertion.
- **Downstream checks:**
  - **Composite score:** stays in [0, 1].
  - **Construction credit:** still capped at 0.999.
  - **`CodingVerifier.checked`** (the unchanged-candidate cache): keyed by identity, so unaffected.
  - **Receipts:** use only passed/executed, so unaffected.
  - **Repair runs:** reconciliation's probes gain the same signal. Re-score its recorded run to
    confirm nothing regresses.
  - **Gaming:** a regression to an earlier failing line does not reset the counter, because the
    best score is kept.
  - **Tests:** a linear probe failing at increasing lines gives an increasing score, and a
    failure inside a helper uses the caller's line.

#### F3. The worker token budget charges the whole prompt again every turn

- **Evidence:**
  - MLX reports `prompt_eval_count` as the full prompt; r3 used 13–42K tokens per turn. The cached
    count, `prompt_eval_cached_count`, is computed in `mlx_transport.py` and then ignored, because
    `ChatResponse` drops extra fields.
  - Ollama also reports the full prompt; the supervised-v3 run averaged 18K per turn.
  - The Snake intake allows 1.5M tokens per run and 4M per task. At about 34K per turn, a run stops
    on `STOP_TOKEN_BUDGET_EXHAUSTED` near **45 turns**, far below its 128 iterations. The task
    budget runs out around attempt 3.
  - r3's three runs already used 1.43M.
- **Fix (class):** charge work, not repetition.
  - The adapter knows when a request extends the previous one (exact replay, no compaction).
  - It should report `cached_input_tokens`: the MLX cached count where present, and otherwise the
    previous prompt length when the prefix is unchanged.
  - The budget check then counts input minus cached plus output. Add the new quantity as a
    `RunState` property used only by budget checks; leave `total_tokens` as is for reports and
    evaluations.
- **Downstream checks:**
  - The task aggregate in `campaign_execution` uses `total_tokens`; switch it to the same property.
  - Benchmark and trajectory metrics keep `total_tokens`.
  - Old streams have `cached_input_tokens` set to 0, so they are unchanged.
  - Paid providers have their own cost budget.

#### F4. After a failed attempt, the supervisor is not told why the checks failed

- **Evidence:** r3, goal event 53.
  - The worker's evidence exceeded 24 KB, because the full diff of a from-scratch app was included.
  - `CampaignGoalWorker.observe` then compacts it to `measured_summary` (pass/fail per check), a
    1,000-character `stop_summary` ("CODING_REQUIRED_CHECK_CIRCUIT_OPEN…") and an artifact ID.
  - `last_verification`, the failing probe lines and assertion messages, is dropped.
  - Gemini then made 19 inspect decisions, paging `game.js` 120 lines at a time. Lines 1–120 were
    read three times. The goal ran out of budget at decision 26.
- **Fix:**
  - When compacting, drop `patch`, which is retrievable by artifact. Always keep a bounded
    `last_verification` (about 6 KB) and the failing acceptance lines.
  - Make the stop summary's failure category honest (see F11).
- **Downstream checks:**
  - `goal_guidance._latest_attempt` already reads `last_verification` and `failure_summary`, so
    the worker handoff improves too.
  - Gemini request size: about 6 KB more, inside the 48 KB request cap.
  - Test: an attention observation for a 40 KB patch still carries the failing lines.

#### F5. `retry` is offered after the task's attempts are used up, and choosing it blocks the goal

- **Mechanism:**
  - `allowed_actions` offers RETRY whenever the worker status is `attention`.
  - The coding reducer refuses `retry` once `attempts == max_attempts`; Snake has 3.
  - `CampaignGoalWorker.execute` raises `ValueError`, and `_run_worker` turns that into
    `GoalStopped("blocked")`. It does not reject the action and let Gemini choose again.
  - Gemini is never shown the attempts used or remaining.
- **Fix:**
  - Show `attempts_used`/`max_attempts` and `review_attempts_used`/`max_review_attempts` in the
    worker evidence.
  - Remove RETRY and VERIFY from the allowed actions when exhausted; a revised `delegate` is still
    allowed, as a new assignment version.
  - Turn a refused worker transition into `ActionRejected` rather than a terminal block.
- **Downstream checks:**
  - `goal_allowed_actions` is used by delivery validation and the Gemini prompt.
  - The `delivered` precondition is unchanged.
  - Tests: retry after max attempts is not offered; a forced retry is rejected with feedback and
    the goal is not blocked.

#### F6. Supervisor allowances are lifetime counters that progress never resets

- **The counters:**
  - **`failures`:** HTTP errors, invalid envelopes and interrupted inference. Consecutive only when
    the intake opts in, and Snake did not.
  - **`action_rejections`:** never reset.
  - **`research_failures`:** never reset.

  At 3, the goal blocks. A long build makes dozens of decisions, so three glitches spread over four
  hours end it even when the work is progressing. r1 ended exactly this way.
- **Fix:**
  - Count all three as consecutive, reset by a valid decision or an executed action.
  - Make that the default for new intakes. Existing goals keep their journaled policy. The intake
    binding hash is unchanged, because the serializer already omits the default.
  - Show the remaining allowance, not only the used count, in the prompt.
- **Downstream checks:**
  - The coordinator's exhaustion checks and the morning report.
  - Replaying terminal journals gives lower counters but the same terminal phase. Pin that.
  - `max_decisions` and cost still bound a goal that keeps erring.

#### F7. Gemini's response schema does not state the domain's rules; drift is found live, one case at a time

- **Evidence:** all 7 rejected envelopes in the Snake runs broke rules that exist only in
  `SupervisorDecision.__post_init__` (which arguments each action takes) or in dataclass bounds.
  `ClarificationRequest` still has no bounds in the wire schema.
- **Fix (by construction):**
  - Move the action-to-arguments map into one module-level table used by `__post_init__`, by the
    generated prompt text, and by the tests.
  - Give `ClarificationRequest` a bounded wire schema.
  - Add a test that every domain bound (lengths, counts, enums) appears in
    `DecisionPayload.model_json_schema()`.
  - Optional: a per-action `anyOf` response schema. Gemini would then be *unable* to emit a
    mismatched envelope. This needs one schema-acceptance call (cents), so it waits for an
    authorized run.
- **Downstream checks:** decision decoding, the code-owned reason echo (`_CODE_OWNED_REASONS`),
  and prompt size.

### P1

| # | Finding | Evidence | Fix |
|---|---|---|---|
| F8 | Multi-file read allows 4 files; the app has 6. Every attempt's **first action failed**, followed by 6 single reads. | All 3 r3 runs, sequence ~9–17 | Allow 8 paths. Read in order up to the 24K total and name the files that were not read, rather than refusing the call. |
| F9 | A response with several tool calls is rejected entirely; 3 in a row stop the run (`MODEL_INVALID_RESPONSE`, retry streak). | r2, r3: "model proposed 5/3 tool actions" | Execute the first call, replay only that call, and add a code-owned note that the rest were dropped. |
| F10 | Each Gemini decision reserves 8,192 output tokens; the largest output across 198 decisions was 1,645 (p99 1,547). The reservation is about $0.11 against a mean charge of $0.018, so goals stop with money left. r3 left $0.10, about 3 decisions. | Usage scan | Set the decision `maxOutputTokens` to 4,096 (2.5× the observed maximum). Do the same for review after measuring it. |
| F11 | `worker_failure` treats unknown codes as PROVIDER. `CODING_REQUIRED_CHECK_CIRCUIT_OPEN`, `CODING_AUTOMATIC_CHECK_BLOCKED`, `MODEL_DEADLINE`, `REVIEW_NO_NEW_EVIDENCE`, `REVIEW_*_CIRCUIT_OPEN` and the `REPORT_*` codes are all reported as "provider failure". RETRY requires `intent.failure` to match exactly. | `failure_recovery.py` | Map every reason code explicitly; unknown codes become UNKNOWN. Add a test that every reason-code literal in `src` has a mapping. |
| F12 | Runtime rejections (`BLOCK_CIRCUIT_OPEN`, `BLOCK_PERMISSION_DENIED`) reach the model as "No new observation". | `ollama_model._no_observation` | Record the rejection reason as the tool result (see F1). |
| F13 | Calling a probe right after a write re-runs it on the same candidate the verifier just checked: 30–60 s of Chromium each time. | r3: write, verify, `check_acceptance_*` on an identical identity | When the workspace identity matches the verifier's last run, return the stored full result, labelled as such. |
| F14 | A failing check's tool output shows "trusted acceptance line 97" without the text of that line. Only the verifier summary names it. | Diagnosis D | Append `acceptance_failure_line` in `measured()`. Keep the end of Playwright errors within the detail budget. |
| F15 | Reviewing a from-scratch build reads the whole app twice (each new file plus `CAMPAIGN_PATCH.diff`), at 3,000 characters per paid call. Review history keeps 32 KB, less than the app. Never exercised on an A3B build. Estimated about 20 calls (about $0.5) per pass for Snake, more for reconciliation. | `review_obligations`, `REVIEW_HISTORY_BYTES` | Count diff hunks of wholly added files as covered by reading the file itself. Prove the build review path offline (Phase A3) before any paid run. |
| F16 | The coordinator attaches the evidence store only via `isinstance(GeminiSupervisor)`; any wrapper silently loses it. Diagnosis C, patched at the call site. | `repository_supervisor.coordinator` | Give the evidence-store attachment a port method and forward it through wrappers. Add a test with `RecordedSupervisor`. |
| F17 | No-new-evidence counts only patch changes and new reads. A check result with a new failure fingerprint is not evidence. | `RequiredChecksAwareModel` | Count a new failure fingerprint as new evidence. |
| F18 | Verification runs every Chromium probe after every write: 21–45% of wall time in r3 attempts 1 and 3. | Per-run timing | Run the probes concurrently (one container each), or run only cheap checks per write and full probes when a check is called or completion is claimed. Decide after Phase A data. |

### P2: structure and process

- **S1. There is no inventory of stop rules and no whole-run test.**
  - The single `test_repository_supervisor` integration test is a happy path: a scripted worker
    writes correct code on the first try.
  - Nothing tests a realistic trajectory, meaning typos, failing checks, many edits before a probe
    flips, multi-call replies, 40+ turns, retries and a review rejection, through all the gates.
  - This is the root cause of the whack-a-mole. Phase A below fixes it.
- **S2. Behaviour is switched by profile-name string comparisons.** `inference_profile ==
  "qwen-native-v1"` turns on about 10 separate behaviours across `build_coding`,
  `campaign_execution` and `campaign_review`. Collect them into one code-owned profile record so a
  profile's behaviour can be read in one place. This is low risk but not urgent.
- **S3. Layering.** The goal path drives the older campaign harness. Repository goal → campaign →
  coding task → run gives four state machines for one assignment. I do not recommend a rewrite now;
  the fixes above are local. Revisit once builds deliver.
- **S4. Relative credential paths.** `launch` reads `.loopforge/secrets.env` relative to the
  current directory, so starting it from elsewhere fails. Resolve the path from the repository root.
- **S5. Per-step overhead.** While the worker runs, each coordinator step (about every second)
  rebuilds `CampaignGoalWorker`, replays the run's events, hashes the workspace and republishes
  evidence. This is cheap but pointless. Cache by the coding-event sequence.
- **S6. Process.**
  - A month of work landed as one commit (`bf1b287`, +41K lines).
  - 26 process docs were added between Oct 20 and 24.
  - Several fixes this week were introduced and then caught live.
  - Phase A makes "caught live" rare. Commit in reviewable units.

### What checked out, so it is not re-litigated

- **Review admission (176 KB envelope):** fits Snake comfortably (app about 25 KB, diff about 28 KB).
- **Delivery and verification:** sound, as are receipts, contract hashing and candidate identity.
- **The today-fixes:** measured checks, repeat-neutral verdicts and construction credit behave as
  described. I found no consumer that reads a successful-but-failed check as a pass.
- **Worker time:** model-bound (66–90% model). The slow turns are 6–8K-token reasoning bursts, and
  each compaction costs one 40–50 s re-prefill. This is not a correctness issue.
- **Model capability on builds:** A3B's from-scratch ability is still unknown, because every
  attempt was cut short by F1–F3. Qwen 3.8 27B built Snake from scratch on Oct 21 under
  `qwen-bounded-v3`.

## 4. Second- and third-order map of today's three fixes

| Fix | Direct effect | Next consumer | What it exposes |
|---|---|---|---|
| Measured checks (`3bd298d`) | A check circuit no longer opens | The run lives longer | F2 no-progress at 8 strikes; F3 token budget at about 45 turns |
| Repeat-neutral verdicts (`31efc40`) | Re-checks are not strikes | The run lives longer | Same as above |
| Construction credit (`883c87c`) | New files count as progress | Once the files exist, flat again | F2 |
| Evidence store (`3bd298d`) | Gemini sees originals (24 KB) | The baseline uses most of the 24 KB | F4 still hides why checks failed; inspection still pages |

## 5. Plan

### Phase A: test infrastructure first, free and offline

1. **Gate inventory test.** Enumerate every stop, limit and circuit from the code into one table,
   asserted in a test, so a new gate cannot be added silently.
2. **Scripted whole-run trajectories, no Docker.** These go through the real `Runtime` with all
   production wrappers, using a fake verifier whose score sequence mimics a build. The cases:
   - 60 turns
   - 3 edit typos
   - multi-call replies
   - 10 edits before a probe flips
   - check calls on an unchanged candidate

   The assertion: the run ends on verified success or on the intended bound. Add a goal-level
   variant: worker failure → retry → attempts exhausted → revised delegate → review
   rejection → delivered.
3. **Recorded-run replay, with Docker but free.** Re-drive the recorded r1–r3 worker actions
   through the current harness and report where each would stop today.
4. **Build dress rehearsal, with Docker but free.** Run the delivered Qwen 3.8 Snake app
   (`.loopforge/tasks/snake-qwen38-fresh/.../app`) through the full goal path:
   - the frozen r3 intake
   - real probes
   - a scripted Gemini supervisor
   - the review path with a scripted Gemini reviewer

   This generalises `scripts/prove_goal_gates.py`, which hard-codes reconciliation files.

### Phase B: P0 fixes, each with its downstream tests

F1, F2, F3, F4, F5, F6, F7. Each is landed only when the Phase A suites pass and its listed
consumers are covered.

### Phase C: P1 fixes

F8–F17. F18 is decided from Phase A3 timing.

### Phase D: free local smokes

1. Worker-only MLX run on both starters.
2. The same on Qwen 3.8 27B as the control.
3. Success means verified success, or failure only on model outcomes.

### Phase E: paid runs

One supervised run per app, each under a new authorization.

## 6. Decisions for the user

1. **Supervisor context size.** The 48 KB request cap and 24 KB working evidence keep decisions
   at about $0.02 but force paging. With F4 fixed, paging should mostly disappear. Option: raise
   the cap to 96 KB and working evidence to 48 KB, at about $0.04 per decision. Recommendation:
   fix F4 first and re-measure.
2. **Counter default.** Make supervisor counters consecutive by default for new intakes (F6).
   Recommended.
3. **Review cost for builds.** Count added files' diff hunks as covered by reading the file (F15).
   This halves review reads and keeps full source coverage. Recommended.
4. **Verification cadence (F18).** Concurrent probes keep today's semantics; per-write cheap checks
   change them. Decide after Phase A data.
