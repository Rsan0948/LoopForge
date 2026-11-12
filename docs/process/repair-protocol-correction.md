# Repair protocol correction

Date: 2026-10-22

## Implemented

- Immediate model-attempt failure feedback is reconstructed from persisted events
  in fresh coding contexts. It remains lower-trust evidence, states that no action
  executed, and clears after an accepted proposal. This includes rejection by
  outer wrappers, not only Ollama parsing failures.
- Repair instructions are phase-specific. An existing decision no longer receives
  an unconditional instruction to record another best guess.
- The advertised legacy decision schema narrows to `status: unable` after a
  decision exists, matching the independent guard. Code-owned schema adaptation
  cannot change registered tool identities.
- `revisable_repair_decisions: true` is a frozen opt-in for new configurations.
  A revised decision replaces the hypothesis but investigation counts remain
  anchored to the first decision in the unchanged candidate/advice cycle.
  Existing permissions, edit validation, budgets, and verification still apply.
- Fresh state includes bounded exact recent successful edit arguments, explicitly
  distinguished from proof that a defect is fixed.
- Specialist advice whose candidate basis differs from the current candidate is
  explicitly marked historical, with instructions to consult current edits and
  verification before repeating old next steps.
- Repair prompt versions advance to 1.8.0 feedback variants.

Existing trial intakes and terminal runs were not changed or resumed. The new
configuration field is omitted when false, preserving existing serialized intake
hashes. Use a newly frozen successor to enable revised decisions:

```json
{
  "inference_profile": "qwen-state-v1",
  "repair_escalation": true,
  "repair_decisions": true,
  "force_repair_attempt": true,
  "revisable_repair_decisions": true
}
```

This fragment supplements the required repository, scope, budget, and task
configuration. It is not a standalone intake or permission to launch paid work.

## Verification

- Full suite: **3,113 passed, 67 skipped**, 146.69 seconds.
- Ruff lint and formatting: passed.
- Pyright: zero errors and warnings.
- Import contracts: both passed.
- New HTTP-boundary regression responds based on the actual serialized request:
  missing-decision rejection reaches the next request; successful proposals clear
  feedback; repeated-decision rejection reaches the next request; phase schemas
  match the guard; two edit/verification cycles complete.
- Revision regression allows a changed hypothesis while rejecting an extra read
  after the original allowance is exhausted.
- Configuration tests reject revision authority without repair-decision authority.
- Schema-adapter tests reject renaming registered tools.

## Local model probe

A single bounded local Qwen request exercises the previously failed first-run
boundary with corrected feedback, instructions, and schema, retaining the same
model and sampling settings. The original archived requests remain unchanged.
Artifacts are under `.loopforge/diagnostics/repair-protocol-fixed-probe/`.
No Gemini request was made. The corrected request returned `apply_edits` with
an exact matching source SHA256 and the PDF row-count change from six to five.
Ollama reported 84.28 seconds total, 7,131 input tokens, and 180 output tokens.

The proposal was validated for path confinement, source hash, strict exact-edit
schema, edit count, and unique replacement targets, then applied only in a
separate diagnostic candidate copied from the preserved pre-repair checkpoint.
The original benchmark workspace and trial records were not modified. The
existing pinned container and immutable `check_project` command were used before
and after the edit:

| Checkpoint | Project tests |
| --- | --- |
| Before local model edit | 6 failed, 7 passed |
| After local model edit | 3 failed, 10 passed |

This is one live boundary probe, compared with the archived repeated failures.
It demonstrates a valid, useful model edit after the protocol correction; it is
not a repeated-trial qualification or proof of full application completion.
Remaining failures concern the jobs API, document upload, and calculation path.
The two-cycle continuation proof is the offline HTTP-boundary regression, not
an unattended live completion claim.

Saved artifacts include `live-response.json`, the corrected request and prompt,
`verification.json` with complete before/after output, and the disposable
`candidate/`. `probe.py` and `verify.py` preserve the experiment procedure.
`probe.py` makes one local inference request if explicitly run; `verify.py`
refuses to overwrite an existing diagnostic candidate. The new request size is
expected to differ from the original: it includes the correction being tested.

The optional revisable-decision mode was covered by regression tests but was not
required for this successful live probe. No existing campaign was restarted,
and no server deployment was performed.

The original diagnosis and pre-change reconstructed requests are historical
artifacts. Their reconstruction script depends on the corresponding frozen
source version; running it against modified source is not a replay of the old
implementation.
