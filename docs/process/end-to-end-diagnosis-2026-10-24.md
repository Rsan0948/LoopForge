# End-to-end build diagnosis — 2026-10-24

The user asked for both benchmark apps to be built end to end from their untouched starters
(Snake first, then reconciliation), on MLX, supervised by Gemini, and installed. Snake was
tried three times and never delivered; $1.40 of its $1.50 was spent. Reconciliation was
not started; its $3.00 is untouched. The user stopped the experiment and asked for this
audit. No paid or local inference is running.

## 1. What happened

| attempt | worker outcome | goal outcome | spend | defect exposed | fixed in |
|---|---|---|---|---|---|
| `snake-a3b-mlx-2026-10-24` | Wrote 5 files, ran checks once, then `STOP_STALLED_NO_PROGRESS` after 8 equal scores | blocked: 3 rejected retry envelopes | $0.320 | (a) Writing new files of an unfinished app cannot raise the acceptance score, so construction counted as no progress. (b) `retry` allows only `recovery`; Gemini added an assignment, and the rejection reason was not echoed. The recovery limit (≤1000 characters) was not in Gemini's schema. | `883c87c` |
| `…-r2` | Built all 6 files (score 0.42→0.50), then ran each check to read its failure; each re-check of the unchanged candidate was a strike; stalled | blocked: 3 assignment objectives > 1500 characters | $0.314 | (c) Re-reported verdicts counted as attempts. (d) Gemini's objectives exceed the bound even when the reason is echoed. | `31efc40` |
| `…-r3` | 3 worker attempts, best 0.72 (launcher fixed; gameplay and records still failing); every attempt ended on `CODING_REQUIRED_CHECK_CIRCUIT_OPEN` | budget exhausted after 25 decisions (19 `inspect`) | $0.765 | (e) A check that ran and failed was a tool failure, so iterating on it opened the tool circuit. (f) `launch` never gave Gemini the research store, so every record was a 2,000-byte excerpt and it paged source one ~50-line page at a time. | `3bd298d` |

Every run in this sequence ended on a harness rule, never on an infrastructure fault.
Across all of this session's trials, the only deliveries were repairs of a preserved app
(`supervised-v3`, `supervised-mlx`, `review-coverage`).

**The process failure is mine.** I launched a paid, supervised from-scratch run without
first running the worker alone on a from-scratch task, which is free. Each paid attempt then
found one more gate, which I fixed and relaunched. This is the "prove problems one at a time"
pattern the user had already rejected. A free local smoke of each starter would have exposed
(a), (c) and (e) at no cost.

## 2. Root causes, by class

### A. Stop rules calibrated on repair work (worker)

Repair work starts from a mostly passing app, so a score that does not improve really means
no progress. A from-scratch build breaks that assumption in three places:

| rule | behavior on a build | status |
|---|---|---|
| No-progress counter (`min(8, max_iterations)`; score must exceed the best) | Each new file of an unfinished app leaves the probe score flat | Construction credit, 0.02 per distinct changed file, bounded by `max_changed_files` (`883c87c`) |
| A re-verified unchanged candidate is scored again | Diagnostic check runs counted as attempts | `repeat` verdicts are neutral in the reducer (`31efc40`) |
| Tool circuit (consecutive failures) | Running a failing check is the normal fix loop, but it opened the circuit | A check that ran and failed is a measured result under the native profile; sandbox errors, timeouts and policy denials still count (`3bd298d`) |
| No-new-evidence (8 turns with no changed patch and no new read) | Unchanged; bounds read and check loops | Kept |

All three fixes are unit-tested (allowed and denied cases) but **unproven live**. The first
validation run (a free local MLX smoke on the snake starter) was stopped at the user's request.

### B. Supervisor contract drift (Gemini envelopes)

All 7 rejected supervisor envelopes in the Snake runs come from domain rules that Gemini's
response schema and prompt did not state:

- Which arguments each action takes lives only in `SupervisorDecision.__post_init__` (for
  example, `retry` takes only `recovery`). The prompt says only "supply only arguments for the
  chosen action."
- `RecoveryIntent` (20–1000 characters, 1–12 IDs) is a dataclass, so the schema carried no
  bounds; fixed in `883c87c`. `ClarificationRequest` has the same gap and is still open:
  topic/category ≤80 characters, question ≤2000.
- Assignment objective ≤1500 characters: Gemini exceeded it 5 times. It was raised to 4000, and
  Gemini still overshot that once.
- Three consecutive rejected envelopes block the goal, and each rejected envelope is billed.

These were found one live failure at a time. There is no test that the wire schema states every
domain bound, so the next drift will also surface live.

### C. Evidence plumbing regression (introduced today)

The `launch` command wrapped Gemini in `RecordedSupervisor`. `RepositoryGoalService.coordinator`
attaches the research store only to a bare `GeminiSupervisor`, so the provider never had it.
Every record reached Gemini as a 2,000-byte excerpt. That caused the 19 inspects in r3 and
the 3 paging decisions in the supervised MLX trial. It is fixed in `3bd298d` with a test,
but the coordinator's `isinstance` check is still the trap: any other wrapper repeats the bug.

### D. Feedback the worker receives

- A failing check's own tool output sometimes ends in a bare `AssertionError` at "trusted
  acceptance line 97" with no message. The verifier's summary includes the failing line
  (`acceptance_failure_line`), but the check tool output does not.
- Playwright timeouts are cut at the 2,000-character detail budget inside the call-log frames,
  before the line that says why the click failed ("element is not visible").

### E. Worker capability on a dense from-scratch spec

In 3 attempts (63 actions), the A3B worker never passed gameplay and records. The failures were
specific spec misses: immediate reversal, one turn per tick, `#start-btn` visible after game
over, newest-first history. On 2026-10-21 the dense Qwen 3.8 27B, with thinking off, wrote all
six files and passed every probe on its first attempt. A3B delivers repairs reliably. Whether
it can build these apps from scratch is **not established**: the gates in A cut every attempt
short, so this is confounded, not proven.

### F. Cost and time

- Each supervisor decision is about 45 KB (about 12K tokens), about $0.03. With the excerpt bug,
  one worker retry cycle cost about $0.25 in inspections.
- In r3 attempts 1 and 3, verification took 21–45% of worker wall time: every write reruns all
  three Chromium probes.

## 3. Today's changes that need a second look

- `3bd298d` measured checks: a failing check is now a successful tool result. I found no
  consumer that reads a successful check as a pass. The runtime skips verification after a
  successful read-only tool, but the verifier would have returned the cached verdict for that
  unchanged candidate anyway.
- `883c87c` construction credit raises failing scores by up to 0.02 × `max_changed_files`
  (0.36 at 18 files). Scores are compared only against themselves, but any report that shows
  scores now mixes in this credit.
- `31efc40` adds `repeat` to `VerificationFailed`. Old streams decode as `False` (tested).
- Earlier today: the multi-file read defect (`0153995`), the reasoning-cap crash (fixed before
  use) and the `launch` evidence store (C) were all bugs I introduced and later caught live.

## 4. Recommended plan, in this order and with no paid runs until step 3

1. **Run free local from-scratch smokes, worker only, for both starters.**
   - Run each starter on MLX at the current commit, using
     `LOOPFORGE_COMPARISON_INTAKE=<intake> scripts/run_engine_comparison.py --smoke --only mlx`.
   - Success criterion: the worker reaches verified success, or fails only on model
     outcomes, not on harness rules.
   - Also run the same smoke on Qwen 3.8 27B (bounded-v3) to separate model capability (E)
     from the gates (A).
2. **Close the supervisor contract, by construction instead of case by case.**
   - Generate the action-to-arguments table from the domain map into the prompt.
   - Give `ClarificationRequest` a bounded wire schema.
   - Add a test that every domain bound appears in the wire schema.
   - Attach the research store wherever a supervisor is wrapped, instead of relying on an
     `isinstance` check.
3. **Then one supervised from-scratch run per app**, within the remaining authorizations: Snake
   has $0.10 left, so it needs a new authorization; reconciliation has $3.00.
4. **Feedback improvements:**
   - Put the failing probe line in the check tool output.
   - Keep the end of Playwright errors within the detail budget.
5. **Verification cadence for builds:** run cheap checks after each write, and the full browser
   probes only on an explicit check or a claimed completion.
