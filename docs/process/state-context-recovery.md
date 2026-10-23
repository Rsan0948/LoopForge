# State-context trial recovery

The user requested stopping the state-v1 trial, repairing its control failures,
and preserving the program so a successor can continue. The owned runner was
interrupted; cancellation was journaled. Its close waited on the in-flight worker,
so the owned process was terminated and the interrupted worker reconciled through
the reducer. The goal and final worker run are cancelled. Production services were
not stopped. Gemini spending for that trial was $0.018074; cumulative prior
journal estimate is $0.843114.

## Preserved implementation

`.loopforge/tasks/reconciliation-recovery/resume-checkpoint.json` records a
read-only predecessor archive, its SHA256, every candidate file hash/mode and the
isolated snapshot revision `20dac9bde7bdb91f49b246f5ee40bc824034ea5b`.
The model's current application bytes were copied without assistant repairs.
The source and snapshot were verified equal before/after preservation.

The prepared successor intake retains the original pinned image, allowed paths,
trusted acceptance probes, checks, task limits and Gemini ceiling. Only provenance
and historical failures were updated in CONTINUATION.json. When launched, run.py
copies the preserved app into a fresh worker relative to its implementation
baseline, so model-created tests remain editable rather than becoming protected
baseline tests. The cancelled predecessor is never reactivated. Preparation does
not start a goal deadline or perform model inference.

## Repairs

For fresh coding context, feedback is now a pure bounded projection included in
every context packet, with a deterministic SHA256 version and explicit startup
completion. The packet projects verify/repair phase from current verification.
The context event journals this feedback before inference. All diagnostic/source
content retains untrusted authority. The feedback action is omitted from this
profile's model catalog, and direct redundant invocation receives
CODING_FEEDBACK_ALREADY_PRESENT. Legacy profiles retain their feedback tool.
The fresh objective no longer says review_feedback FIRST or repeats an unconditional
startup check instruction. An unrelated LAUNCH.md instruction is omitted for this
profile. Phase instructions distinguish initial verification from repairing the
current failure.

Exact edit batches are fully validated, then rejected with CODING_EDITS_NO_CHANGE
if the final bytes equal the input. This covers an identical replacement and edits
that cancel each other. The write adapter is never invoked for such a batch.
Genuine edits retain existing path, fingerprint, atomicity and permission checks.

Fresh-context automatic retries receive prior run IDs from the task's durable
linked events. If a prior run exhausted the no-progress allowance and its final
journaled patch fingerprint equals the current candidate, CODING_STALLED_RETRY
stops before inference. Rebuilding a model wrapper does not reset this guard.
A changed recovery patch permits another bounded attempt; deliberate new frozen
successors are separate experiments. No new authority or larger limits are granted.

## Verification and limits

Hermetic tests exercise inline feedback delivery/version changes, untrusted
provenance, allowed source/edit/check tool exposure and denied redundant feedback,
identical and cancelling edit rejection, genuine edits, durable stalled retry
rejection before inference, and recovery after a changed patch. The full serial
coding/review/revision/acceptance scenario runs under both legacy and fresh
profiles; it checks the actual fresh objective and journaled feedback packet.

These tests establish control behavior, not that Qwen will complete the app. The
successor has not been launched and the application has not passed acceptance.

Final validation: 3,038 tests passed, 67 skipped; Ruff lint/format, Pyright (zero
errors) and both import-layer contracts passed. The first standalone environment
probe failed with Chromium TargetClosedError while the full suite was active;
that log is retained as preflight-attempt-1.log. An unchanged readiness probe passed
after the suite finished, followed by expected candidate project/API failures.
The concurrency timing is observed, not proof of the browser failure's cause.
No memory/process/network limits were expanded to obtain a passing readiness probe.

The successor launch inputs are frozen in reconciliation-recovery/frozen-manifest.json.
The package is prepared without creating a new goal or calling a live model.
