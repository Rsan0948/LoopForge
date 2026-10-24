# Harness audit after the supervised trial — 2026-10-24

The user's goal is to start using the loop soon, with MLX as the likely engine. This audit
records what the delivered supervised trial and the matrix-v4 data show about speed and
reliability, and what remains before day-to-day use. Every finding has its evidence; the work
items are in the checklist at the end.

## 1. The supervised trial (roadmap 6.6)

`.loopforge/tasks/reconciliation-forced-attempt/supervised-v3-2026-10-24/` delivered:
- 6 decisions, in order: prepare, reproduce, inspect, delegate, verify, deliver.
- About 7 minutes wall time.
- $0.401 of a $1.50 cap: supervisor $0.138 (6 calls), review $0.263 (12 calls).
- The worker took 12 actions.

The patch was checked by hand: the test corrections match CONTRACT.md arithmetic, and the CSS fix
holds with its `overflow-x: hidden` lines removed. Details are in roadmap item 6.6.

**The supervisor is the best speed lever found.** The same seed with the same worker took
20–60 calls and 4–35 minutes unsupervised, and 3 of 8 runs failed. With a specific Gemini
assignment, it took 12 actions and about 7 minutes. This is one run, not a rate.

## 2. Where the time goes (matrix v4: 8 trials, 262 model calls)

| finding | evidence |
|---|---|
| Model inference is 79–91% of wall time; tools and checks are minor | Per-trial sums of model-call seconds against trial wall time |
| Decode of output tokens dominates model time | e.g. rep1-ollama-cold: 64,896 output tokens, about 1,580 s of 1,911 s model time at about 41 tok/s; total prefill 246 s |
| `read_file` is 55% of calls (145 of 262) and 47% of output tokens (89.5K) | Output tokens per call grouped by the tool chosen; mean 617 tokens of reasoning per read |
| Nearly every call makes one tool call | 1 of 262 calls used two tools, although the adapter accepts several |
| One runaway call: 16,384 output tokens, no tool call | rep1-mlx-warm; about 5.5 minutes lost at the output cap |

**Speed work, in order of measured payoff:**
- **S1. Fewer read round-trips.** Let a single read call return several files (one `read_file` call
  taking a list of paths), and say in the tool description that several read-only calls may go in
  one turn. Seed the first turn with what the model always reads first: the failing-check output,
  which it already fetches through `review_feedback`, and the source files named in its tracebacks,
  within the working budget. Expected effect: a large share of the 145 reads disappear, each saving
  about 600 output tokens (about 13 s at 45 tok/s) plus a prefill.
- **S2. A reasoning cap on MLX.** The agentic server wrapper is LoopForge code, so it can force the
  thinking block closed at a token budget. Ollama has no equivalent. That turns a 16K runaway
  into a bounded cost.
- **S3. Supervision by default.** The delivered run's precise assignment cut worker calls by 2–5×.

## 3. Reliability defects found during the launch

- **B1. The goal runner hangs silently after an unexpected exception.**
  - In `GoalExecutionService._loop` (`src/loopforge/entrypoints/goal_runner.py`), `drive` errors
    other than `ValueError`, `OSError`, `RuntimeError`, `httpx.HTTPError` and `sqlite3.DatabaseError`
    escape. The `finally` block records `status="offline"` with an empty error, and the service
    thread dies.
  - The trial's `run.py` raises only when an error is recorded, so it waits forever.
  - Observed on the first launch: an `AssertionError` killed the thread, and the process stayed alive
    with no error recorded.
  - Fix: catch every `Exception`, record its type, and keep the queue alive.
- **B2. Retired goals stay armed.**
  - The execution service drives every armed, non-stopped goal directory under its root. That is
    by design for the server queue.
  - `review-handoff-2026-10-23` was operator-stopped and superseded, but its runner record stayed
    armed. The new trial's service therefore picked it up first; `run.py`'s directory assertion
    refused it before any goal step.
  - It was disarmed by hand (`runner-retired.json` records why).
  - Fix: operator stop and successor creation disarm the predecessor in code, plus a test.
- **B3. The engine check is hard-coded to Ollama.** `dependencies()` probes
  `http://127.0.0.1:11434/api/tags` for the configured model, which blocks an MLX-served goal.
- **B4 (operator tooling, not the harness).** The trial watcher script missed the run.log decision
  lines; only the exit notice arrived. The next watcher should poll the goal journal, not tail the
  log.

## 4. Quality notes from the delivered patch

- **Q1. The app-owned 409 test was removed.** The worker corrected the wrong-job upload test to the
  contract's 422 but did not add the same-job, different-file 409 case. The independent API probe
  still covers 409, so behavior is guarded. The previous review (`full-a3b-2026-10-23`) flagged this
  exact gap; this review did not. Worth adding to the reviewer's coverage obligations: when a test
  assertion changes status code, the old code path must still be tested somewhere.
- **Q2. Redundant CSS.** The `overflow-x: hidden` lines do nothing; the padding changes fixed the
  overflow. The probe checks `scrollWidth`, which hidden overflow can satisfy in some layouts.
  Add a "no element beyond the viewport" check to the browser probe, so masking cannot pass in a
  future run.

## 5. Making MLX the production engine

The MLX path today is lab code:
- `scripts/run_engine_comparison.py`'s `Transport` and `mlx_payload` translate the adapter's Ollama
  chat request into an OpenAI-style request to `scripts/mlx_server_agentic.py`.
- The prompt is rendered exactly by Ollama's own template through
  `.loopforge/mlx-lab/renderer-oracle` (a Go binary).
- The model runs from the lab's Python 3.12 environment on port 18181.

It passed 6 of 9 lab trials.

**M1–M5. Steps to production:**
- **M1. An adapter.** An `httpx` transport module in `adapters/` with the request translation,
  response normalization and strict response validation. Unit tests use recorded exchanges,
  with no network.
- **M2. Config.** An `engine: ollama | mlx` field in the campaign config, owned by code, plus the
  MLX endpoint. The dependency check (B3) probes whichever engine is configured.
- **M3. A service.** A launchd agent for the MLX server, like the Ollama one. It runs the pinned
  lab environment and the renderer oracle, and is loopback-only.
- **M4. A per-engine working budget.** MLX decode is flat (48–50 tok/s through 30K), so its budget
  can rise once depth beyond 30K is measured (`scripts/bench_engine_depth.py`, extended to 48K and
  64K). Ollama stays at 32K.
- **M5. Validation.** The smoke on MLX through the production path, then one supervised trial.

## 6. Day-to-day use

- **U1. Launching takes hand-written scripts.** Every trial so far needed:
  - a hand-written 29 KB `repository-intake.json`;
  - a `prepare.py` (snapshot, funding record, frozen runtime copy);
  - a copied `run.py`.

  `loopforge-repository-supervisor create|run|status|accept` exists, but it has no runtime freezing,
  request capture or intake template. To start using the loop on a new repository, the user needs:
  - `init` to write a starter intake;
  - one `launch` command that freezes, arms and runs.

## Checklist

- [ ] B1 runner catches all `drive` exceptions and records them (tests: allowed path keeps
      queue, failure path records error and exits run scripts)
- [ ] B2 stop/successor disarms the predecessor's runner (tests)
- [ ] B3 + M2 engine-aware dependency check and config
- [ ] M1 production MLX transport adapter (tests on recorded exchanges)
- [ ] M3 MLX launchd service
- [ ] S1 multi-file read and first-turn seeding (tests; replay measurement)
- [ ] S2 MLX reasoning cap in the agentic server (lab measurement)
- [ ] M4 depth benchmark to 64K, then per-engine budget
- [ ] Q1 reviewer obligation for changed status-code assertions; Q2 probe element-bounds check
      (probe change needs a newly frozen intake)
- [ ] M5 MLX smoke and one supervised trial (needs spend authorization)
- [ ] One-command launch: `create`, freeze and `run` without hand-written per-trial scripts
