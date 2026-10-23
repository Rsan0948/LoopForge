# Rich working session trial — 2026-10-23

## Purpose

Test whether preserving useful working history and reducing evidence paging lets
Gemini delegate and Qwen complete the existing reconciliation repair. This is an
experiment, not a claim that richer context guarantees autonomous completion.

## Implemented

- New opt-in `qwen-session-v1` coding profile: 98,304-token configured window,
  4,096-token output reserve. The previous profiles retain their window settings.
- Durable session reconstruction from original action/result journal events:
  exact attempted edits, source reads, successful results and tool failures.
  Historical source remains explicitly historical after edits. Reconstruction
  works across process restarts; it does not depend on an in-memory chat cache.
- The working-history allowance is 40,000 serialized UTF-8 bytes of whole records,
  rather than three source excerpts and three edits. Omitted event sequences are
  explicit. Latest runtime state and verification diagnostics remain separate.
- Supervisor and worker handoff receive complete stored research originals when
  they fit a 24,000-byte working-evidence allocation. The latest baseline is kept
  first; duplicate artifact retrievals are deduplicated. Partial/omitted records
  are labeled and originals remain in the integrity-checked research store.
- Supervisor plans are prompted to carry established findings, unsuccessful
  approaches and unresolved questions. Assignments may use already-authorized
  directory prefixes, allowing the worker to determine related edit locations.
- Session instructions allow the worker to select reads, edits and fixed checks;
  they do not prescribe the old repair/verify phase script. Forced-edit and repair
  decision gates are disabled for this trial. Gemini supervises and reviews;
  the nested repair-specialist loop is disabled for this new trial.

Provider request admission still checks the complete serialized request, including
tool definitions and reserves. No repository content, model output, historical
result or supervisor suggestion becomes runtime authority. Scope, container,
fixed commands, independent behavioral acceptance and review remain enforced.

This is a bounded, exact-history implementation. It does not yet generate semantic
summaries of arbitrarily long histories. Once the history allowance fills, whole
records can be omitted with explicit identities. Original events remain on disk.
The experiment does not change inference engines or establish MLX performance.

## Funding and trial

The user explicitly authorized **$3 additional** after being asked for a new
shared Gemini allowance. This is a separately funded goal with an immutable
$3 ceiling shared across its paid roles. Earlier spending is not erased and the
prior terminal goal is not reactivated.

The previous linked ledger records $0.588604 spent. This is local recorded spend,
not a measurement of the user's total Google account usage or remaining credits.

Trial directory:
`.loopforge/tasks/reconciliation-forced-attempt/rich-session-2026-10-23/`

The exact preserved application is snapshotted from the preceding trial's input.
No application repair is made by the primary assistant. Acceptance probes, fixed
checks, environment and allowed edit paths are compared with the preceding
configuration during preparation. Obsolete experiment-specific procedural text
is removed from the objective; the product requirements remain.

## Verification and results

- Full suite: 3,187 passed, 67 skipped in 179.37 seconds.
- Additional profile wiring/provider-boundary coverage: 95 focused tests passed.
- After the final supervisor request-fit adjustment: 28 focused tests passed.
  The full-suite count above predates that final adjustment.
- Ruff lint/format, Pyright, and both import architecture contracts passed.
- Container contract preflight, credential presence and local model availability
  passed. The live supervisor will execute environment readiness and baseline
  reproduction through the normal goal flow.
- Offline replay of the preceding final supervisor state retained three complete
  artifacts, including the full baseline, in a 41,451-byte request. Six other
  distinct artifacts were explicitly omitted. This is richer retained evidence,
  not unlimited context or proof of better model decisions.

Launched PID 54682 using the frozen runtime. See the trial's funding receipt,
frozen runtime hashes, captured supervisor/worker requests, run log and final
report. Starting the runner is not proof of successful repair.

This trial changes context, instructions and specialist composition together. It
can demonstrate practical improvement, but is not a controlled attribution of
which individual change caused an outcome.


## Initial live observations

- Gemini prepared the environment, reproduced failures, inspected source once,
  and delegated on **decision 4**. No evidence retrieval/paging decisions were
  needed before delegation. Previous trial: 25 decisions without delegation.
- Actual Gemini spending through delegation: **$0.073256**; no outstanding paid
  reservations at that observation. New shared ceiling: $3.
- Qwen worker run: `run_0c049638cafe`. First observed actions read
  `app/reconciliation.py` and `app/server.py`.
- Captured first request: 43,005 bytes; next request: 50,269 bytes. Actual Ollama
  options use `num_ctx=98304`, `num_predict=4096`. The second request retains the
  complete first read in historical action/results, with no history omissions.
- Measured failures in this checkpoint concern idempotent upload status,
  reconciliation totals and mobile layout overflow. They are not a claim about
  every previous checkpoint's failures.
- Runner remains active in the background. No completed repair, passing final
  verification or successful independent review is claimed at this checkpoint.

Inspect with:

```sh
.venv/bin/python .loopforge/tasks/reconciliation-forced-attempt/rich-session-2026-10-23/observe.py
```

The runner writes `final-report.json` on a terminal outcome. Current progress is
also in `run.log`, `latest-state.json` and the append-only journals.
