# Qwen 3.5 4B bounded code-generation pilot

Outcome: no usable submission; no proposed source or test edits applied.

## Scope and setup

One local Ollama request to installed `qwen3.5:4b` (Q4_K_M), based on LoopForge
`5fdc3d5`. Task was the narrow boolean-validation portion of audit F21:
reject non-bool VerificationResult.repeat and VerificationFailed.repeat/inconclusive,
retain current behavior for booleans, preserve all other validation, and add focused
allow/deny and codec compatibility tests. This did not attempt the broader codec audit.

The prompt provided the complete verifier port, the relevant event class, contribution
rules, exact allowed paths, constructor examples, codec API, invalid inputs, and test
expectations. Output was constrained to JSON search/replace proposals plus a test module.
The model had no shell/filesystem tools. A detached disposable checkout was prepared;
this is not a claim of OS-level sandbox isolation. No model-generated code was executed.

Configuration: temperature 0, thinking disabled, context 16,384, output cap 5,000 tokens.
This tests one patch-generation configuration, not the autonomous WorkBuddy harness or
all configurations of this model.

## Evidence

- Local response: 89.71 seconds; 1,751 prompt tokens; 5,000 generated tokens;
  finish reason `length`. Reported load duration approximately 3.49 seconds.
- The response was incomplete JSON and failed parsing; no patch application was attempted.
- Inspection of its visible proposed edits found existing VerificationFailed score
  validation removed despite the explicit preservation instruction.
- Its inconclusive check used `if self.inconclusive and not isinstance(...)`, allowing
  invalid falsey values such as 0, None, and empty containers through.
- Proposed tests included nonexistent import locations for VerificationResult and
  JsonEventCodec. The test content was also truncated.
- Baseline reproduction confirmed string `repeat="false"` is accepted at construction
  but rejected when the encoded event is decoded.
- Existing event, JSON codec, and partial-progress suites: 285 passed before any changes.
  These are baseline checks, not validation of the model's unsuccessful proposal.

Raw local artifacts (prompt, response, caller script) remain in
`/tmp/loopforge-qwen-small/`. Temporary artifacts are not durable repository evidence;
this report records the relevant measurements and findings. The unused worktree was
removed after the experiment.

## Assessment

This attempt did not demonstrate savings: specification/review overhead produced no
acceptable patch, and repairing it would require rewriting meaningful parts of the result.
It is not evidence that every small-model workflow will fail. Thinking was disabled and
JSON mode did not enforce a full output schema; different prompting or generation settings
could behave differently. Those alternatives were not tested, and no success rate or exact
Codex token savings can be inferred from this single sample.

Do not expand this configuration to the backlog based on this result. No correction loop,
paid provider call, broader hardening run, or app trial was launched. All audit findings
remain open. Only this pilot report was added to the primary checkout.
