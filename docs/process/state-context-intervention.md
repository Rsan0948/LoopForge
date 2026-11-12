# Fresh context intervention

The reconciliation trial was cancelled after repeated source rereading without a
repair. Its original intake, manifest, candidate workspace, events and evidence
remain under `.loopforge/tasks/reconciliation/`. The terminal goal is not resumed.
The stop record is `operator-stop.json`; recorded Gemini spending was $0.182116.
No candidate application repairs were made during this intervention.

## New opt-in profile

`inference_profile: qwen-state-v1` selects fresh coding and review requests.
Every action request rebuilds system instructions and current state. Previous
assistant messages, tool conversations and thinking are not replayed. There is no
history compaction or model-generated continuation summary. Legacy profiles keep
their existing behavior; use a newly frozen successor configuration to test this
profile, rather than altering the cancelled trial's frozen manifest.

The base packet preserves the objective, current control state, human instructions,
latest verification and latest observation as whole items. Coding adds current
required-file hashes/change status, missing changes, remaining token/iteration
budgets, six recent action selectors/outcomes and up to three exact source
observations within 10,000 UTF-8 bytes. Cached source requires a matching current
full-file hash and a permitted workspace path. Changed files invalidate it.
Changed status is not a claim that a deliverable works.

Review adds coverage, remaining read allowance and three recent exact excerpts.
Its full source/evidence store and mandatory report evidence remain available
outside chat. Earlier exact excerpts can be retrieved with `observed_excerpt`.
Repository content and observations retain untrusted authority; the packet cannot
grant permissions or change budgets. Packet assembly is recorded through the
runtime's context event before inference. Durable event history is retained.

Coding/review have a 65,536-token physical window and 48,000-character message
limit. Full serialized request admission still includes the tool catalog and
output/framing reserves. Oversize current state fails closed without trimming or
compaction. A fixed, run-scoped protocol correction may survive one malformed
response and clears after the next valid action. No malformed output is replayed.
The reporter retains its separate large evidence envelope.

## Qualification limits

Offline tests cover request size stability, discarded old observations, retained
human instructions, run isolation, correction lifecycle, oversize rejection,
trust preservation, unchanged legacy profiles and stale-source invalidation.
No live successor trial has been launched. Whether fresh context improves task
completion, latency or repeated-read behavior still requires that experiment.
Fresh requests do not by themselves eliminate poor action selection or guarantee
that a model fixes the next failing check.

Validation after hardening: full pytest suite 3,034 passed, 67 skipped; Ruff lint
and formatting passed; Pyright reported zero errors; both import-layer contracts
passed. These are framework checks, not a live qualification of the new profile.

## Continuation trial launched

The user subsequently authorized a new trial starting from the stopped model's
actual code. `.loopforge/tasks/reconciliation-state-v1/` holds the frozen successor.
The original cancelled goal remains unchanged. A read-only, SHA256-recorded archive
of the entire predecessor is `artifacts/stopped-reconciliation.tar.gz`; the separate
candidate Git snapshot is `ce77c00814bb6c9c7dbd569d18327f7ebc4b9af8`.
`continuation-manifest.json` records byte-for-byte candidate seed hashes and modes.

The successor uses the prior prepared assignment with the original acceptance
probes and pinned image. Original implementation files stay relative to the
original starter baseline; model-created tests remain editable rather than being
accidentally frozen as protected existing tests. Only a provenance/failure handoff
was added to the successor source baseline. No assistant candidate repairs were
made. Environment readiness passed; candidate project/API checks reproduced the
unfinished application's failures before launch.

Run `run_07eb4e752962` started with `qwen-state-v1` and actual Gemini supervision.
Its first action was `review_feedback`; first serialized request was 13,224 bytes,
with a 65,536-token window, 4,096 output reserve and zero compacted turns. Initial
state packet was journaled before inference. The new Gemini ceiling is $1.25;
prior cumulative journal estimate $0.825040 plus this ceiling remains below $5.
This is a continuation experiment, not a from-scratch success or delivery claim.
