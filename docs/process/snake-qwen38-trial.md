# Qwen 3.8 27B autonomous Snake retry

**Final result: a fresh-start trial delivered with runtime verification and fresh review. Qwen wrote all application code; the primary assistant repaired only the framework before that final run.**

User requested this experiment on 2026-10-21 to qualify LoopForge's loop, not obtain an assistant-finished game. The primary assistant must not author, repair or transplant candidate application code. Earlier terminal goals and their ledgers remain immutable.

The active assistant-built `apps/snake` and the two task-owned installed launchers were removed. Their prior bytes remain only in historical evidence under `.loopforge/tasks/snake/retired-delivery`; the worker cannot access that location through its confined source tools.

## Before the run

- Verified installed `qwen3.8:27b-q4_K_M` digest `25b843619e944cd0ae6069f94ff4e5e26a16e109ccbc0a66a0f05979ed70098e`, tools/thinking capabilities and reported 262,144 context capacity.
- Added operator-selected `qwen-bounded-v2`: 65,536 context, 8,192 coding output, reasoning disabled and no reasoning replay. Existing profiles retain their defaults.
- Coding composition now uses the selected profile's actual context and reasoning settings rather than hardcoded 32K/thinking settings. A versioned code-owned native-call prompt specifies one action and bounded edits; runtime rejection of multiple actions is unchanged.
- Optional model commentary over 4,096 UTF-8 bytes is omitted whole from replay, preserving exact native action and tool result. Mandatory source/authority is never truncated to make admission succeed.
- The opt-in review profile permits a 56,000-byte full reporting request inside 65,536 context, reserving 3,072 output tokens and framing. Mandatory-source coverage, full request measurement and fail-closed overflow remain enforced. Original reporting limits remain unchanged for earlier profiles.
- Live local smoke calls demonstrated a single native action and schema-constrained reporting. Mocked unit tests exercise admitted requests, denied oversized requests, exact source retention and legacy behavior.

## Frozen experiment

Intake, model identity and comparison limitations are in `.loopforge/tasks/snake-qwen38/`. Original unfinished Git revision, objective, trusted acceptance probes and Docker restrictions are unchanged. Model, profile and framework repairs change together, so this is a system-level retry rather than a controlled model-only A/B.

Gemini supervisor maximum additional estimate is $0.75. Earlier cumulative journal estimate was $0.451748, within the existing $5 authorization. Local Qwen has no per-token API charge. No production campaign is restarted or changed.

Only a terminal delivered goal with its runtime receipts may be reported as autonomous success. Blocked/budget-exhausted outcomes must remain failures with their actual checkpoints; the primary assistant will not finish the app manually.

## First retry outcome and framework recovery

The first Qwen retry generated all six files and diagnosed/repaired the queued-turn and repeated-server-launch failures itself. Its last worker attempt passed every frozen acceptance check. It recorded no model context-limit errors; the maximum admitted serialized coding request was 54,741 bytes in its 65,536 envelope. It recorded one zero-tool response while stuck on review admission, followed by the normal no-new-evidence stop. It did not complete unattended: mandatory review evidence measured about 74KB after its repairs, above the fixed 56KB reporting request ceiling. The trial was cancelled through the operator control after preserving its checkpoint, and charged $0.109614.

A separate framework recovery selected `qwen-bounded-v3`. Coding stays at 65,536; only the separate reporter gets a 98,304 context and 88,000-byte serialized request ceiling with its fixed output/framing reserves. The prior v2/legacy limits are retained. Mandatory evidence is not omitted; oversized packets still fail closed. A read-only real-candidate preflight measured 73,902 bytes and admitted the complete packet. Focused tests221passed and strict type checking passed.

The recovery copied the original Qwen-generated candidate byte-for-byte into an isolated prepared-goal checkout, then ran the actual Gemini supervisor, Qwen controller and fresh Qwen reviewer. No ScriptedModel results, manual candidate fixes or outside advisory approval were injected. It reached `delivered` with runtime `verified:true`; final candidate hashes equal the original Qwen checkpoint. Gemini charge $0.015364. This proves the framework recovery/verification path, **not** a fresh-start unattended build.

A final independent fresh-start trial was subsequently run in `.loopforge/tasks/snake-qwen38-fresh`, using the original unfinished revision, same objective and same acceptance probes, with bounded-v3 selected before creation. It has no seeded candidate. Its delivered outcome is recorded separately below.

## Final fresh-start qualification: delivered

The final trial reached **`delivered` with runtime verification**, starting from the original unfinished repository. It used the actual Gemini supervisor and local `qwen3.8:27b-q4_K_M` controller/reviewer throughout. No candidate was seeded; the primary assistant wrote **zero application code** and made **zero framework changes during this final trial**. The framework file hashes match their pre-run manifest.

- First coding attempt: six native `write_file` actions; runtime `success_verified`.
- Fresh source review: runtime `success_verified`, empty findings and unresolved lists. All mandatory source and patch intervals were covered.
- JavaScript/Python/Bash syntax and frozen real-browser gameplay, saved-records and launcher probes passed both coding and review verification. Runtime receipts were recorded; advisory prose was not substituted for approval.
- No model failures, invalid tool-call responses or context-limit failures. Maximum recorded controller request was 48,009 serialized bytes; no live history compaction was required in this bounded benchmark.
- Approximately **24 minutes 39 seconds** from frozen goal creation to its terminal export.
- Gemini journal charge **$0.066198** for the fresh trial. Today's three trials total **$0.191176**, and the full conversation estimate is **$0.642924**, within the existing $5 authorization. These are conservative journal estimates, not an account bill.
- The generated app remains only in its isolated trial checkout. `apps/snake` and both previously installed user launchers remain removed. Nothing was published, installed, committed, pushed or deployed.

Evidence under `.loopforge/tasks/snake-qwen38-fresh/`: `result.json`, `experiment.json`, `framework-manifest.json`, `runtime-verification-and-review.json`, immutable `goal/` journals and frozen intake, plus final framework checks. The earlier failed trial and separate prepared-candidate recovery remain under their own directories and retain their original terminal outcomes.

This qualifies **one bounded local autonomous build** after framework repairs. Model and framework settings changed together; it does not establish a model-only causal improvement, broad Phase 5 qualification, or production rollout readiness. The first retry's report-capacity failure was not hidden by the successful recovery or fresh qualification.

Final framework validation: **3,015 passed, 67 skipped** with the coverage gate passed. Ruff, formatting (234 files), strict Pyright (0 errors), both import contracts and whitespace checks passed. All trial runners stopped after their terminal states; no paid or local trial inference remains active.
