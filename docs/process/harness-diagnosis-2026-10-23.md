# Harness diagnosis — 2026-10-23

Scope: the live `rich-session-2026-10-23` reconciliation trial (PID 54682), its
captured worker/supervisor requests and event stores, and the process journals
from 2026-10-20 onward. Read-only investigation; no harness, application, or run
state was changed.

## 1. Arc so far

- Goal: Gemini supervises, local Qwen 3.8 27B codes, overnight unattended work.
  Benchmark: the manufacturing reconciliation app.
- Snake trial: Qwen wrote all application code and its last worker attempt passed
  every frozen acceptance check. It failed to complete unattended only because of
  harness review admission. The model is capable of this class of work.
- Reconciliation trials (2026-10-21 → 2026-10-23): each trial fixed one
  control-flow defect and exposed another — endless rereads, seven identical
  repair decisions, lost rejection feedback, stale specialist advice, and a
  supervisor that spent its entire allowance paging source before delegating.
- Pattern: every fix adds a new mechanism (stall detectors, forced decisions,
  retry blocks, admission gates) and the next trial is terminated by one of those
  mechanisms. ~150 uncommitted files, 3,187 tests.

## 2. Live trial state (observed 2026-10-23 ~16:15 UTC)

Running ~3 hours, ~$0.54 of a $3 ceiling spent, 60 goal events, 17 supervisor
decisions. The remaining application work is small and already diagnosed by
Gemini: two Qwen-authored tests have wrong expectations (`110000` should be
`100000`; a wrong-job upload expects `409` where CONTRACT.md mandates `422`), plus
mobile CSS overflow. The idempotent re-upload `200` fix is already applied.

| Run | Actions | Outcome |
| --- | --- | --- |
| `run_0c049638cafe` | 28 reads, **0 edits** (`reconciliation.py` ×7, `server.py` ×9) | `CODING_NO_NEW_EVIDENCE` |
| `run_3ebb20cb5453` | 0 actions | `CODING_STALLED_RETRY` — blocked instantly, same patch |
| `run_15a244b26df5` | 10 reads, 1 edit | `MODEL_CONTEXT_LIMIT` |
| `run_f847c95a7c4f` | 8 reads so far | in progress, same trajectory as run 1 |

Gemini's latest retry rationale claims the worker edited `server.py`,
`test_calculations.py` and `styles.css`. Only `server.py` was edited.

## 3. Primary findings

1. **Reasoning is disabled.** Worker payload has `think: False`. A 27B model is
   debugging multi-file failures with 27–44 output tokens per turn and no
   planning space.
2. **Not a real conversation.** Every turn is rebuilt as `[system, one ~57K-char
   user message]`. No assistant tool-call / tool-result turns. Qwen is trained on
   native multi-turn tool chat; here it must reconstruct its own history from a
   document.
3. **Admission counts bytes as tokens.** `measurement_basis:
   utf8_byte_upper_bound`. Run 3 was killed at "98,303 tokens"; a same-size
   request in run 4 was actually **25,495 tokens** per Ollama. The 98K window is
   effectively ~27K, and a run with ample room was terminated.
4. **History cap evicts what was just read.** `session_history(max_bytes=40000)`
   (`src/loopforge/entrypoints/campaign_session_context.py:9`). Real input
   plateaus at ~25K tokens from turn ~11; older reads drop, Qwen forgets them and
   rereads. This is the reread loop.
5. **Prompt budget is inverted.** ~33.5K chars (≈60%) is the supervisor's
   `model_inference` evidence blob; the worker's own working history gets ~7.4K
   chars. Every item carries trust labels and policy prose, burying the task.

Interpretation: the problem is not only permissions. The harness sits between the
model and its own memory and reasoning. Safety controls (scope, sandbox, fixed
checks, budgets, independent acceptance) can remain; items 1–5 are context
delivery, not safety.

## 4. Recommended next step

A $0-Gemini local A/B: replay one stuck run-1 boundary directly to Ollama with
`think: true`, native multi-turn tool messages, real token accounting and a slim
prompt, and observe whether Qwen makes the edits.

## 5. Additional findings — downstream, state, logic, assumptions, mechanical

Investigated 2026-10-23 ~16:15–16:30 UTC. All reproduction ran in disposable
scratch copies; the live trial's workspace, journals and budget were not touched.
Run 4 ended and run 5 (`run_e8443bf79c43`) started during this investigation;
run 5's first five actions are again reads only.

Totals across all five worker runs (~5 hours): **49 actions = 48 `read_file`, 1
`apply_edits`, 0 checks, 0 searches, 0 diffs.**

### Summary table

| ID | Finding | Kind | Impact |
| --- | --- | --- | --- |
| B1 | Sandbox `nofile=128` makes the browser acceptance probe fail regardless of code | Mechanical / environment | **Task unwinnable as configured** |
| B2 | App is ~6 lines from passing every check | Downstream state | The remaining work is trivial |
| B3 | No KV-cache reuse: 97–98% of each turn is prompt prefill | Mechanical / performance | ~4 min/turn instead of ~15–30 s |
| B4 | `temperature: 0.0` + `think: False` on all recent Qwen profiles | Configuration | Greedy repetition, no planning |
| B5 | Session profile is structurally guaranteed to hit `MODEL_CONTEXT_LIMIT` | Logic | Runs 3 and 4 killed |
| B6 | Fresh worker runs are told "phase: verify / no failure recorded" | State logic | Contradicts real failures |
| B7 | Retry after a zero-edit stall is dead on arrival | Logic deadlock | Wasted decision + run |
| B8 | Retry hypothesis doesn't replace the assignment objective | Downstream state | Worker follows stale instructions |
| B9 | Supervisor tells the worker edits were made that weren't | Incorrect assumption | Worker hunts for nonexistent edits |
| B10 | First assignment asserted a nonexistent bug in `reconciliation.py` | Incorrect assumption | Run 1 read it 7× |
| B11 | Probe failures cite line numbers of source neither model can see | Visibility | Nobody can diagnose B1 |
| B12 | Goal `failures` is a lifetime counter (2 of 3 used) | State logic | Next malformed Gemini reply ends the goal |
| B13 | Gemini supervisor capped at 48 KB (~12K tokens) of a 1M window | Configuration | Forces paging, inflates decisions |
| B14 | Assignment validation feedback is lossy; failures still charged | Mechanical | $0.09 lost on two envelopes |
| B15 | Paid reservations ~4.7× actual charge | Mechanical | "Budget exhausted" with money left |
| B16 | Stale "build from scratch" objective in every worker and supervisor prompt | Incorrect assumption | Pulls toward rereading CONTRACT/PDFs |
| B17 | Minor mechanical defects | Mechanical | See list |
| B18 | Review gate is untested downstream | Risk | Snake trial failed here |

### B1 — The browser acceptance probe cannot reliably pass inside the harness sandbox (P0)

The coding container is built with `SandboxLimits(max_memory_bytes=2 GiB)`
(`src/loopforge/entrypoints/campaign_coding.py:996`), inheriting the default
`max_open_files=128` (`src/loopforge/adapters/local_sandbox.py:69`), which becomes
`--ulimit nofile=128:128`. The supervisor's reproduction sandbox uses the same
default (`src/loopforge/entrypoints/goal_investigation.py:552`). Chromium plus a
uvicorn server under 128 file descriptors stalls.

Reproduced with the pinned image, exact harness flags and exact
`-I -B -c exec(compile(..., '<trusted-acceptance>'))` wrapper:

| Workspace | `nofile` | Result | Time |
| --- | --- | --- | --- |
| current candidate | 128 | 6/6 runs: Playwright 30 s timeout at line 65, 65, 65, 65, 65, 69 | ~32 s |
| current candidate | 1024 | 3/3 runs: reaches line 87 (`Mobile overflow`, the genuine app defect) | ~3 s |
| candidate + fixes (B2) | 1024 | **all five checks pass** | — |
| candidate + fixes (B2) | 128 | browser probe: **timeout at line 66**, the exact failure recorded by the harness | ~32 s |

The harness receipts match (`check_acceptance_browser` `"seconds": 32.12`). The
measured baseline happened to reach line 87; every post-baseline verification
recorded a line-66 timeout. That shift is environment flakiness, not a regression
from the worker's `server.py` edit (baseline and candidate behave identically
under both limits).

Consequence: even a perfect worker patch would be rejected by the browser
criterion most of the time. This is a code-owned workload contract (AGENTS.md
rule 13); raising the descriptor limit for this workload is a capability-contract
change that should be made explicitly by the operator, not by any model.

Reproduction artifacts:
`/private/tmp/claude-501/-Users-rubensanchez-Developer-LoopForge/55bebefa-2ebb-4af3-988b-109376a5f700/scratchpad/probe/`
(`base/`, `cand/`, `fixed/`, `probe_0.py`–`probe_2.py`).

### B2 — The application is about six lines from complete

Starting from the current candidate (which already contains the correct
idempotent-200 `server.py` edit), these changes pass `check_documents`,
`check_project` (13 passed), and all three trusted acceptance probes at
`nofile=1024`:

- `app/tests/test_calculations.py`: expect `supported_total_cents == 100000` and
  `variance_cents == 20000` (the test arithmetic was wrong; CONTRACT.md line 11
  and the implementation agree).
- `app/tests/test_api.py`: wrong-job upload expects `422`, not `409`
  (CONTRACT.md line 27).
- `app/static/styles.css`: `overflow-x: hidden` on `html, body`; `min-width: 0` on
  layout children; `table-layout: fixed` and `overflow-wrap: anywhere` on evidence
  cells and document items.

These were applied only to a scratch copy to establish solvability. Per the trial
rules, they were **not** applied to the live candidate. Gemini's diagnosis of the
remaining work (seq 58/65) is correct against the contract.

### B3 — No KV-cache reuse; prefill dominates every turn

Ollama metrics for run 4: `prompt_eval_count` equals the **full** prompt each turn
(16.7K → 26K tokens) and `prompt_eval_duration` is 97–98% of `total_duration`
(~105 tokens/s prefill, 2.5–4.2 min/turn; decode 27–44 tokens at ~6.5 tok/s).

Cause: state mode (`conversation_mode="state"`, `ollama_model.py:490–510`) rebuilds
`[system, single user message]` each turn. Items that change every turn (the
runtime-policy plan text, the latest observation, the state packet) come early in
the user message, so the shared prefix with the previous request ends after about
2.8K characters. An append-only conversation reuses the cached prefix and only
prefills the new turn delta.

The adapter already implements append-only native tool-call/tool-result history
(`conversation_mode="history"`, `_append_turn_delta`, `ollama_model.py:511–560`);
the session profile simply selects `"state"` (`ollama_profiles.py:43–53`).

### B4 — Greedy decoding with reasoning disabled

Every profile since `qwen-bounded-v2` uses `temperature=0.0` and `thinking=False`
(`ollama_profiles.py:43–87`). The original `coding-qwen-v1` candidate used
`temperature=0.6` with thinking. Qwen-family model guidance warns that greedy
decoding causes degraded output and endless repetition, which matches the
observed reread loops. With reasoning off, a 27B model plans multi-file debugging
in ~30 output tokens per turn. B3 makes thinking cheap to re-enable: decode time is
~4–7 s of a ~4 min turn today.

### B5 — The session profile cannot fill its own history without being killed

- Admission: `input_token_bound = serialized_request_bytes`
  (`ollama_model.py:389–398`), because no caller ever passes
  `request_token_counter` (`ollama_model.py:126`; grep finds no other use).
- Ceiling: `98,304 − 4,096 − 1,024 = 93,184` "tokens", which in practice means bytes.
- Fixed request overhead is ~50 KB (system, objective, ~33.5 KB supervisor
  guidance, check definitions, prior-attempt evidence, 16 tool schemas). The history
  cap is 40,000 bytes (`campaign_session_context.py:9`). The latest observation
  is also duplicated as a standalone item.
- 50 KB + 40 KB + latest observation > 93 KB, so a run that fills its history
  always dies. Runs 3 (98,303 bytes) and 4 (94,869 bytes ≈ 26K real tokens) did.

Ollama reports ~3.4 bytes/token for these requests, so the real window is ~3.5×
larger than the gate allows.

### B6 — Fresh worker runs are told there is no failure

`campaign_state_context.py:216` sets
`phase = "repair" if state.last_verification_passed is False else "verify"`.
`state` is per run, so every new run starts at `None` → `"verify"`. The same
prompt contains `"current_verification": "No pending verification failure."` and
the coding-progress item `"current_check_failure": "No check failure recorded in
this attempt yet"` (`campaign_coding.py:874–880`). The actual failing assertions
appear only inside `previous_attempt_evidence` as an escaped string (`\x0a`
newlines) and inside the supervisor blob. The structured fields contradict
the evidence.

Despite `next_step` saying "If no failure is recorded, check the current candidate
first", the worker never ran a check in five runs. Verification ran only once,
automatically after run 3's single edit.

### B7 — Retry after a zero-edit stall is guaranteed to fail

`_deny_stalled_retry` (`campaign_coding.py:917–950`) blocks any run whose
patch identity equals that of a prior run in the same task that stopped with
`CODING_NO_NEW_EVIDENCE`. A `retry` reuses the coding task, so `previous_ids`
includes the stalled run (`campaign_execution.py:291–297, 364`). If the stalled
run made no edits, the patch is unchanged by definition, and only the worker can
change it. Observed: seq 35 `retry` → `run_3ebb20cb5453` blocked at turn 0
(`CODING_STALLED_RETRY`). The supervisor's `retry` action is effectively
unavailable after the most common failure mode. Only a fresh `delegate` (new task)
escapes, which is what happened at seq 42.

### B8 — Retry hypotheses reach the worker only partially

`worker_guidance` (`goal_guidance.py:12–77`) sets `objective` from
`state.assignment.objective`, the previous assignment. `recovery.changed_hypothesis`
is never included. The retry's plan and rationale do appear as
`latest_supervisor_plan_untrusted` / `latest_supervisor_rationale_untrusted`.
Observed in run 4: the most prominent field still said "Apply three targeted fixes"
(no `test_api.py`), while the plan says "2. Delegate the bounded 4-file assignment",
which describes a future delegation rather than the current task.

### B9 — Supervisor states facts about worker output that are false

Seq 58 rationale: "The previous worker attempt failed … after editing
app/server.py, app/tests/test_calculations.py, and app/static/styles.css". Run 3
made exactly one edit (`server.py`). The supervisor had the evidence
(`patch_constraints: passed (files changed: app/server.py)` and the diff), but
misread it. This rationale is forwarded verbatim to run 4. Run 4 then reread
`test_calculations.py` (×2) and `test_api.py` (×2) without editing. No structured,
code-derived "files actually changed" summary is provided to the supervisor or
the worker; the claim depends on model reading.

### B10 — First assignment asserted a nonexistent bug

Seq 12 instructed: "In app/reconciliation.py, ensure supported_total_cents includes
approved changes_cents". The implementation was already correct and the test was
wrong. Run 1 read `reconciliation.py` seven times looking for it. Gemini corrected
itself at seq 35 after direct inspection. The supervisor delegated a hypothesis
as an instruction after inspecting the baseline output and only one source file
(`CONTINUATION.json`).

### B11 — Failures reference source nobody in the loop can read

Worker check definitions mark acceptance probes `"source_omitted": true`. The
supervisor prompt contains no probe source and no `ulimit`/`nofile` detail. The
failure the worker sees is `File "<trusted-acceptance>", line 66 … Timeout 30000ms`.
Neither model can learn that line 66 is "wait for the document list to be empty",
and neither can see sandbox limits. B1 is therefore undiagnosable from inside the loop.
Omitting probe source prevents test-gaming; a code-owned excerpt of the failing
line (not the whole probe) would keep that property while making failures actionable.

### B12 — One more malformed Gemini reply ends the goal

`GoalState.failures` increments on every `CallFailed` (`domain/goals.py:477`) and
never resets. `max_failures=3`; seq 30 and 63 used two. `_infer`
(`application/supervisor.py:203–212`) then stops with "Supervisor recovery
allowance exhausted". This is a lifetime limit on schema slips, not a
consecutive-failure limit.

### B13 — Gemini supervisor input capped at 48 KB

`MAX_REQUEST_BYTES = 48_000` (`adapters/gemini_supervisor.py:34`), also used by
`gemini_repair.py` and `gemini_review.py`. This is ~12K tokens for a
model with a ~1M-token window. Large evidence is compacted into retrievable
artifacts, which drives `retrieve`/`inspect` paging decisions. The previous trial
spent 25 decisions (all its budget) before delegating. Each decision also
re-sends the whole stateless request (`protocol: stateless-generateContent-v1`).

### B14 — Lossy assignment validation; failures still charged

The wire schema advertises `objective ≤ 1500`, `files ≤ 30`, `evidence_ids ≤ 12`
(`gemini_supervisor.py:80–84`). The domain `TaskAssignment.__post_init__`
(`domain/investigation.py:101–115`) additionally requires unique files/IDs and
safe relative paths, and checks `.strip()` length. Any violation surfaces as
`assignment:value_error` with no indication of which rule failed. Seq 63 cost
$0.039 and seq 30 (unparsed envelope, no location at all) cost $0.052. The 1,500-character objective
cap also forces the supervisor to compress the precise edit instructions it
otherwise writes.

### B15 — Reservations are ~4.7× actual charges

`maximum = (bytes + 4096) × 2 + 8192 × 10` (`gemini_supervisor.py:362–363`).
Observed: reserve 180,464 µUSD against charges of ~38,000–52,000 µUSD. The previous
trial stopped "budget exhausted" with $0.137 unspent because the next
reservation exceeded the remainder. This is conservative by design, but it makes the
last ~20% of every allowance unreachable.

### B16 — Stale creation objective in every prompt

Both the worker's `authorized_human` item and the supervisor's `goal` field still
carry the original build-from-scratch text: "Read that complete contract and
inspect the real PDFs with check_documents before implementation … Deliver
app/contracts.py, app/storage.py, …". The debugging preface
("Complete and debug the preserved implementation") is prepended, but the
contradictory build instructions remain as the highest-trust item. Run 1 read
`CONTRACT.md` twice. Meanwhile `files: []` and `missing_required_changes: []` in
the state packet (deliverables are now baseline files) give no positive signal
about what needs changing.

### B17 — Minor mechanical defects

- `completed_source_reads: sorted(selectors)[-12:]` (`campaign_coding.py:869`)
  sorts alphabetically, then keeps the last 12, which drops the alphabetically
  earliest paths (e.g. `CONTRACT.md`, `app/pdf_documents.py`), not the oldest reads.
- The latest `read_file` observation appears both as its own item and inside
  `working_session_history`, paying for the same bytes twice under the byte gate.
- Three overlapping edit tools (`apply_edits`, `edit_file`, `write_file`) plus
  `repository_map`/`find_symbol` that were never used. 16 tool schemas are re-sent
  and re-prefilled every turn (B3).
- `read_file` returns at most 200 lines / 3,000 characters, so `server.py` takes 2–3
  reads. With ~4.5 KB per history record, the 40 KB cap holds ~9 reads before
  evicting, which feeds the reread loop the stall rule then punishes.
- The stall rule (`CODING_NO_NEW_EVIDENCE`, 8 turns) and history eviction work
  against each other. Eviction makes rereads necessary; rereads of evicted pages hash
  identically, so they count as "no new evidence".
- Initial-review note on method: section 3's prompt sample was selected by hash
  filename rather than mtime. It was from run 4 (not necessarily the newest
  request), but every claim drawn from it was rechecked against other requests.

### B18 — Untested downstream gate: review

No run has reached frontier review in this trial. The Snake trial's last attempt
passed every acceptance check and then failed on review admission. The coding
verifier also runs `preflight_report` review-admission checks before fixed checks
(`campaign_coding.py:670–692`). After B1/B5 are fixed, the next blocker is likely
here and should be dry-run offline against the B2 candidate before another paid
trial.

## 6. Revised recommendation (priority order)

1. **Fix the sandbox contract for browser workloads** (B1): an explicit,
   code-owned `max_open_files` for this workload (e.g. 1024), with an
   allowed/denied test per AGENTS.md rule 10. Without this nothing else can succeed.
2. **Offline gate check** (B2, B18): run the full verifier and review admission
   against the scratch `fixed/` candidate. That proves the pipeline can accept a
   correct patch before any model is involved.
3. **Worker inference** (B3–B5): append-only history mode, thinking on,
   temperature ~0.6, real token counting (`/api/tokenize` or Ollama's reported
   counts) wired into `request_token_counter`.
4. **State/handoff logic** (B6–B9): carry the last task-level verification into
   fresh runs; let `retry` either replace the assignment objective with the
   recovery hypothesis or be rejected up front when the patch is unchanged; give
   both models a code-derived "files changed / checks failing" summary.
5. **Supervisor economics** (B12–B15): consecutive rather than lifetime failure
   counting, a larger request cap, specific validation feedback.
6. Then rerun the local $0 A/B from section 4, and only afterwards a paid trial.

Item 1 by itself would not have produced a pass in this trial, since the worker
never edited the tests or CSS. Items 1–3 together are the minimum for a fair test
of whether Qwen can finish autonomously.
