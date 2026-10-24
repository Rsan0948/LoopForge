# Ollama vs MLX engine comparison — handoff log (2026-10-23)

Living document. Read top to bottom; the **Status** section says what is next.

## Goal

Choose the faster local inference engine for the LoopForge coding worker
(`qwen3.6:35b-a3b`, profile `qwen-native-v1`) **without contaminating the comparison**:
same runtime, same frozen task, byte-identical prompts, cold and warm cache modes for
both engines, and each engine tuned to its best configuration before measuring.

## Status

- [x] Stopped the original lab comparison (`scripts/run_mlx_comparison.py`): its design
      was not representative (see "Defects in the original comparison").
- [x] New harness `scripts/run_engine_comparison.py` (commit `390cfd0`).
- [x] MLX tool-turn cache fix `scripts/mlx_server_agentic.py` (commit `235a539`).
- [x] Stopped matrix `engine-comparison-agentic-20261023-1546` mid trial 1: discovered the
      think-flag cache invalidation below. Not a valid result set.
- [x] Fix think-flag / history-replay cache invalidation for both engines (`919c9df`).
- [x] Initial tuning completed; selected measured settings recorded below.
- [x] Re-run the cold/warm matrix: superseded. The final matrix (`engine-comparison-final-20261023-2011`)
      was stopped by the operator during trial 1; see "Final matrix (stopped)" and
      `roadmap-2026-10-23.md`, which now governs the remaining work.

## Key artifacts

| Path | What |
| --- | --- |
| `scripts/run_engine_comparison.py` | Paired harness: 8 trials (2 engines × cold/warm × 2 reps), alternating order, pinned hashes |
| `scripts/mlx_server_agentic.py` | Upstream `mlx_lm.server` + cache checkpoint for tool-final turns |
| `scripts/mlx_lab.py` | Lab env, pinned MLX model, server launcher (`agentic_cache=`) |
| `scripts/ollama_renderer/` + `.loopforge/mlx-lab/renderer-oracle/renderer-oracle` | Go oracle: Ollama's own qwen3.5 renderer, so MLX gets byte-identical prompts |
| `.loopforge/mlx-lab/artifacts/ollama-qwen35-v0.32.15.go` | Ollama's renderer source (read this for rendering rules) |
| `.loopforge/mlx-lab/runs/engine-comparison-20261023-1524/` | Matrix 1: Ollama cold + stock-MLX cold trials (valid, pre-fix) |
| `.loopforge/mlx-lab/runs/engine-comparison-agentic-20261023-1546/` | Matrix 2: stopped; one partial Ollama trial |
| `.loopforge/tasks/reconciliation-forced-attempt/full-a3b-2026-10-23/` | Frozen task intake used by every trial |

Each trial directory holds `exchange-NNN.json` (exact native request, wire request,
response, wall time), `events.sqlite`, `result.json`, `candidate.patch`, `workspace/`.

## Defects in the original comparison (`run_mlx_comparison.py`)

1. Forced 4,096 output tokens (the working A3B profile uses 32,768; this model's reasoning
   is long), and the transport rejected larger values.
2. Disabled Ollama speculative decoding (`draft_num_predict: 0`), biasing speed against Ollama.
3. 600 s per-trial deadline; the known Ollama solve takes ~400–550 s.
4. Hand-written objectives that named the relevant files (answer leakage).
5. `require_revision=True` (review-requested-changes mode) for fresh tasks.
6. Strict name-based tool-result pairing in `mlx_request` → `Ambiguous or orphan native
   tool result` crashes in multi-turn MLX runs (the `renderer-parity` runs).
7. Its Ollama control failed with `CODING_AUTOMATIC_CHECK_BLOCKED` after one edit. Root
   cause was a LoopForge verifier bug (re-running checks on an unchanged candidate after a
   failed read counted as repeated failures); fixed in `b211a29` with a regression test.

The earlier `renderer-parser-parity` "MLX 2/2" result replayed the final boundary of an
already-solved run (one-file edit, diagnosis in context); it does not measure the task.

## Engine findings

### F1 — MLX: no reusable cache on tool-result turns (fixed)

`mlx_lm.server` stores its pre-generation checkpoint only when the last message is
`user`. Agent requests end with `tool`, so on the hybrid Qwen3.6-A3B (recurrent layers,
untrimmable cache) reuse froze at the last user-final checkpoint (3,166 tokens). Stock-MLX
trial: 845 s vs Ollama 412 s on the same task, both verified successes.
Fix: `scripts/mlx_server_agentic.py`. Replay of the captured requests: cached tokens track
the previous prompt (e.g. 5,890 of 6,933) and latency stays flat.

### F2 — Both engines: think flag and history replay invalidate the prefix cache

From Ollama's qwen3.5 renderer (`Render`):

```go
renderAssistantThinkBlock := isThinking && i > lastQueryIndex
```

- The **current request's** think flag decides how **every earlier assistant turn** after
  the last user query renders (`<think>…</think>` block or none). Flipping the flag
  (LoopForge's evidence-driven thinking did this every few turns) makes the prompt
  diverge at the first assistant turn (~3K tokens in): near-full re-prefill.
- LoopForge replayed history without thinking (`preserve_thinking=False`) and omitted
  thinking or commentary over 4,096 bytes whole. Either way the history no longer
  reproduces the generated tokens, so the cache diverges at the last assistant turn.
- Hybrid model: neither engine can trim recurrent state back to the divergence point;
  they fall back to an older checkpoint.

Measured (matrix 2, Ollama cold, calls 15–34): prefill 0.8–4.7 s on stable turns; 16.6,
23.9, 26.2, 32.5, 40.1 s on calls right after a flip; 5–7 s on think→think turns with
dropped thinking. Separately: one 10,756-token thinking turn (716 s decode) and decode
speed falling with context (31 tok/s @12K → 11–15 tok/s @30K+), which are not cache issues.


## Resumed tuned matrix — 2026-10-23 19:13 local

User requested continuation after the cache and sampling experiments. No earlier
stock-MLX timing is used to declare a winner for the corrected deployment.

### Selected configuration

- Runtime: commit `919c9df`, `coding-native-a3b-v2`, thinking always enabled,
  exact assistant history replay, 131,072 context and 32,768 output tokens.
- MLX: tool-final checkpoint wrapper enabled; default server arguments (no KV
  quantization override); `presence_context_size=2048`.
- Ollama: shipped defaults, including speculative decoding; no repeat-window
  override. Both use the common profile's temperature 1.0, top-p 0.95, top-k 20,
  min-p 0.0, presence penalty 1.5 and repetition penalty 1.0.
- Same frozen task/runtime/acceptance, matched prompt rendering, exclusive model
  lease, isolated candidate per trial. Two repetitions per engine/cache mode,
  alternating order. No paid supervisor/reviewer inference.

Saved tuning evidence under `probe-exact-mlx-warm-1620/mlx-warm/`:

| Probe | Recorded result |
| --- | --- |
| MLX presence window 64, 3 samples | Median 2,316 output tokens / 45.21 s |
| MLX presence window 2,048, 3 samples | Median 1,402 output tokens / 27.31 s |
| Ollama, 3 samples | Median 1,839 output tokens / 77.42 s |
| MLX default server, second request | 22,662 cached tokens; reported throughput 46.55 tokens/s |
| MLX KV8, second request | Zero cached tokens; reported throughput 11.66 tokens/s |
| MLX prefill4096, second request | 22,662 cached tokens; reported throughput 46.42 tokens/s |

These small tuning probes selected settings; they are not end-to-end performance
or success-rate estimates. Weight quantization and penalty-history semantics
still differ, so the matrix compares tuned deployments, not engines in isolation.

### Launch and observation

- 14 focused native-profile/protocol tests passed.
- Pinned task/container and exclusive model-lease preflight passed.
- Matrix: `engine-comparison-tuned-20261023-191314`.
- Launcher PID: `23253`.
- `.loopforge/mlx-lab/current-comparison.json` identifies output/log/PID.
- Matrix `plan.json` pins runtime hashes and settings; `results.json` is updated
  after each trial. Per-trial logs and `exchange-NNN.json` capture progress.
- Source changes during the matrix stop it before the next trial. Avoid changing
  pinned runtime/scripts while measurement is underway.

Full matrix results are pending. Neither a launch nor a partial probe establishes
an engine winner.


### Continuous monitoring

A separate read-only observer checks every completed captured exchange throughout
all eight trials: frozen request settings, rendered prompt-prefix retention,
HTTP/transport failures, tool-call count, output-limit termination and per-call
cache/latency metrics. It samples runtime hashes and event counts every 15 seconds.
It does not change the benchmark inputs or generate inference traffic.

An operator watchdog stops the matrix if frozen source hashes or fixed generation
settings drift. It does not classify ordinary test failures, model protocol errors,
long reasoning or cache compaction as automatic proof of a harness defect.
Those are flagged for review. Source/process identity is checked before signalling
the matrix runner. The matrix's normal cleanup preserves captured evidence.

Artifacts in the matrix directory: `monitor-events.jsonl`, `monitor-status.json`,
`monitor-stop.json` if a configuration violation occurs, and `monitor-report.md`
after all trials finish. Monitor process identities are recorded in
`.loopforge/mlx-lab/monitor-launcher.json`. Monitoring cannot prove absence of every
possible harness bug; its checks and limitations are explicit.

### F3 — Fix for F2 (commit `919c9df`)

Profile `coding-native-a3b-v2` (selected by `qwen-native-v1` + `qwen3.6:35b-a3b`):

- `thinking_policy="always"`: the think flag never changes, so earlier turns never re-render.
- `history_replay="exact"` (`OllamaModel`): assistant thinking and content replayed
  verbatim; no 4 KB omission notices.
- Runtime updates are folded into the tool response instead of a separate user message
  (a user message becomes the renderer's last query and strips earlier think blocks).
- Runtime protocol corrections are mid-conversation `system` messages in exact mode.
- Dense-model profiles are unchanged (their caches trim, so flips are cheap).

Deterministic proof with Ollama's renderer (`scripts/verify_prompt_prefix_stability.py
<trial>`): consecutive prompts of a recorded 34-call trial keep 96.2% mean / 67.0% worst
of the previous prompt as sent, and **100% / 100%** under exact replay.

Live 15-call probes (`run_engine_comparison.py --probe ENGINE warm 15`):

- Ollama: prefill tracks only new tokens (0.4–2.4 s), no 16–40 s re-prefills; thinking on
  every turn costs little on simple steps (45–104 output tokens). 15 calls in 49 s.
- MLX: cached tokens track the previous prompt (e.g. 21,934 of 22,664); short turns
  ~2–3 s past 20K tokens. One full miss followed a rejected multi-tool-call response
  (protocol correction as a user message); fixed by the system-role correction.

### F4 — Admission over-counted and compacted one turn per request (fixed `223a624`)

The ratio bound (bytes/2) overstates real tokens ~1.7x (real ≈ 3.4 bytes/token), so with
the 32K output reserve compaction began at ~53K real tokens of a ~97K budget, and it
removed one turn per request. Each compaction rewrites the prompt prefix, so every later
turn re-prefilled everything: Ollama calls 47–53 took ~150 s of prefill each at ~53K
tokens and the trial hit the 2,400 s deadline. It affects whichever engine's conversation
grows longest, so it biases a comparison.

Fix: the bound is the provider's reported prompt tokens for the previous request plus the
byte delta at 2 bytes/token (both engines report full prompt counts, cached or not); a
count above the bound still fails closed to the strict byte bound; a compacted request
uses the full ratio until re-calibrated; compaction frees headroom to 75% of the budget.

## Tuning (before measuring)

| Engine | Setting | Evidence | Chosen |
| --- | --- | --- | --- |
| Ollama 0.32.15 | Server flags already: flash attention, q8_0 KV cache, MTP speculative decoding (≈76% draft acceptance), batch 2048, 32 context checkpoints (from `~/.ollama/logs/server.log`) | — | shipped |
| Ollama | `repeat_last_n` (presence window) 64 vs 2048 | `tune_engine_sampling.py`, 3 samples: medians 1100 vs 1372 tokens, wide ranges, no clear effect | shipped (64) |
| MLX | `presence_context_size` 64 vs 2048 | same request, 3 samples: medians 2316 vs 1402, ranges 1339–4797 vs 1331–1631; wall 45 vs 27 s; edit-argument fidelity 4/4 exact at 2048 | **2048** |
| MLX | `--kv-bits 8` | `tune_mlx_server.py`: prompt-cache reuse dropped to 0, decode 11.7 vs 46.5 tok/s | off |
| MLX | `--prefill-step-size 4096` | no change (689.9 vs 686.2 prefill tok/s) | default |

Single-request measurements (22.6K-token prompt): MLX fresh prefill ~686 tok/s, decode
~46 tok/s with cache hit; Ollama decode measured 24–35 tok/s at 12–22K context in probes.
Reasoning length varies a lot per sample on both engines; reps are needed.

Quantization differs (Ollama GGUF Q4_K_M vs MLX affine 4-bit): this compares deployments.

## How to run

```bash
# Prefix proof (no inference):
.venv/bin/python scripts/verify_prompt_prefix_stability.py <trial-dir>
# Tuning probes:
.venv/bin/python scripts/run_engine_comparison.py --probe {ollama|mlx} {cold|warm} 15 --output <dir>
.venv/bin/python -m scripts.tune_engine_sampling <exchange.json> 3 64 2048
.venv/bin/python -m scripts.tune_mlx_server <exchange.json>
# Full matrix (8 trials, ~1.5–3 h):
.venv/bin/python scripts/run_engine_comparison.py --output .loopforge/mlx-lab/runs/<name>
```

The harness pins source hashes and stops before the next trial if anything changes; do not
edit `src/` or `scripts/` while a matrix runs. It takes the machine-wide `ModelLease`.

## Final matrix (stopped), 2026-10-23 20:11 local

Pinned at `00e262d`. Trial 1 `rep1-ollama-cold` ran 24 calls, then the operator stopped the
matrix (`operator-stop.json`). 0 of 8 trials completed, so there is no result set. The prefix
cache worked on every call: only the 650–1,150 new tokens were prefilled each time. Decode,
however, collapsed with depth: 31.6 tok/s at 10.8K tokens, 17.7 at 20.9K, 11.7 at 34K and
10.2 at 39.4K. The user's directive was to fix everything identified before benchmarking. The
diagnosis is in `context-management-research-2026-10-23.md` §2 and §7, and the plan in
`roadmap-2026-10-23.md`.
