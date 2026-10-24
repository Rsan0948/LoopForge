# Context management: research, our measurements, and a checkpoint-compaction design

Date: 2026-10-23. Status: research complete; design proposed; nothing implemented yet.
Related: `engine-comparison-2026-10-23.md` (engine matrix), `scripts/compaction_lab.py` and
`scripts/run_compaction_comparison.py` (the earlier, unrun Gemini compaction lab).

## 1. Goal

Keep the local worker (Qwen3.6-35B-A3B) at or above about 20 tok/s decode for the whole task.
Do that by keeping the context small (the user's target is a 32K window). Compact at planned
checkpoints that hold usage under the budget, not reactively when a threshold is crossed.

## 2. What we measured (final matrix, `rep1-ollama-cold`, Ollama 0.32.15)

The Ollama server runs `llama-server -c 131072 -np 1 --flash-attn on --cache-type-k/v q8_0
--spec-type draft-mtp --spec-draft-n-max 2 -b 2048 -ub 2048`.

| call | prompt tokens | output tokens | MTP acceptance | decode tok/s | ms/token |
|---|---|---|---|---|---|
| 7  |  9,766 | 193  | 0.70 | 27.2 | 36.7 |
| 8  | 10,805 |  57  | 1.00 | 31.6 | 31.7 |
| 11 | 15,853 | 583  | 0.52 | 18.2 | 55.1 |
| 15 | 20,887 | 110  | 0.71 | 17.7 | 56.5 |
| 17 | 25,139 | 1035 | —    | 14.9 | —    |
| 19 | 29,825 | 989  | —    | 13.4 | —    |
| 21 | 33,999 | 850  | —    | 11.7 | —    |

Prefill per token also rose, from 1.9 ms (call 7) to 3.0 ms (call 15). The prefix cache was
reused on every call: only the 650–1,150 new tokens were prefilled each time. There was no
thermal or performance warning, and no competing process.

Two effects stack:

1. MTP acceptance depends on the content. Tool-call JSON is accepted almost entirely; free-form
   thinking is accepted at 0.5–0.65.
2. Each token costs more as the context grows. At equal acceptance (0.70), cost went from 36.7 to
   56.5 ms/token between 9.8K and 20.9K tokens.

Effect 2 is far too steep for this architecture. The model has 40 layers, laid out as
`10 × (3 × Gated DeltaNet → 1 × Gated Attention)`, so only 10 layers keep a KV cache that grows.
A rough bandwidth estimate puts the attention KV read at 34K tokens under 1 ms/token on an M1 Max.
The observed slowdown is about 3×. That points to a slow attention-kernel path, not physics. One
external report matches this: a dense scan over the whole KV cache, plus quantized-KV
fallback paths, on Qwen-next-style hybrids ([llama.cpp #28734](https://github.com/ggml-org/llama.cpp/issues/28734),
[rdna-boosts #133](https://github.com/stew675/llama-cpp-rdna-boosts/issues/133)). Neither report
is on Metal, so this has to be measured here. With the current Ollama setup, 20 tok/s is crossed
at about 15–17K tokens, not 32K.

### Measured answer (roadmap Phase 1, `scripts/bench_engine_depth.py`, 2026-10-23 20:38)

Setup: requests captured from the evidence trial at four depths; fixed 256-token output, seed 7,
the profile's own sampling. Ollama ran on a private `ollama serve` with each setting; MLX ran on
the pinned agentic server. Artifacts: `.loopforge/mlx-lab/runs/depth-tuning-2038/` (`depth.json` and logs).
Decode is reported in tok/s, with ms/token in parentheses.

| depth (tokens) | Ollama 32K, q8_0 | Ollama 131K, q8_0 | Ollama 32K, f16 | Ollama 131K, f16 | MLX 4-bit |
|---|---|---|---|---|---|
| 8,808  | 29.5 (33.9) | 30.0 (33.4) | 55.3 (18.1) | 53.7 (18.6) | 53.8 (18.6) |
| 17,429 | 21.0 (47.7) | 21.5 (46.5) | 50.5 (19.8) | 50.1 (20.0) | 48.7 (20.5) |
| 25,139 | 13.8 (72.4) | 13.2 (75.9) | 38.9 (25.7) | 36.3 (27.5) | 47.5 (21.1) |
| 29,825 | 12.4 (80.6) | 12.6 (79.6) | 38.6 (25.9) | 35.8 (27.9) | 49.7 (20.1) |

MTP acceptance at each depth was identical across settings (0.83, 0.82, 0.57, 0.56–0.61), so it
does not explain the differences.

Conclusions:

1. **The q8_0 KV cache was the cause of the slowdown.** At 30K tokens it is 3.1× slower than f16. It
   matches the quantized-KV attention slow paths reported elsewhere. The user's service now uses
   `OLLAMA_KV_CACHE_TYPE=f16`; the original plist is backed up as
   `~/Library/LaunchAgents/local.ollama.serve.plist.bak-q8_0-20261023`.
2. **The allocated window has no measurable compute cost** (32K and 131K are within noise at every
   depth). The 131K allocation is kept, per Qwen's "at least 128K for thinking" guidance. The user's
   32K target is applied as a *usage* budget enforced by checkpoint compaction (Phase 4).
3. **MLX decode is flat with depth** (about 48–54 tok/s through 30K). With f16, Ollama declines slowly
   (about 54 to 36 tok/s). Both stay well above 20 tok/s within a 32K usage budget.
4. **Prefill:** MLX cold prefill is about 1.4–1.55 ms/token (about 700 tok/s). Ollama prefill figures in
   this run are not cold, because consecutive depths share a cached prefix, so they are not compared.
5. **MTP on/off:** Ollama 0.32.15 exposes no setting for its draft-MTP (no environment variable or
   option; the runner launches `--spec-type draft-mtp` itself), so it was not varied.

### Does the allocated window (`-c`) itself cost compute?

This is unresolved and has to be measured. In llama.cpp, the attention pass is sized from the
cells actually used (padded), not from the allocated `-c`, so theory says allocation mainly costs
memory. Community benchmarks do show slowdowns with larger `num_ctx`. However, most of those
slowdowns come from KV spilling out of VRAM (a GPU/CPU split), which does not apply to 64 GB of
unified memory ([CachyOS thread](https://discuss.cachyos.org/t/comparison-ollama-vulkan-vs-ollama-rocm/21789),
[offload guide](https://www.popularai.org/p/fix-ollama-cpu-offloading-slow-inference)). The user's
experience says a smaller window is faster. The benchmark in section 5 settles it for this stack.

There is one conflicting piece of guidance. The official model card advises
"maintaining a context length of at least 128K tokens to preserve thinking capabilities"
([Qwen3.6-35B-A3B card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)). If allocation turns out
not to cost compute, the best of both is: allocate at least 128K and enforce a usage budget of
about 32K through checkpoint compaction. If allocation does cost compute, take the 32K window
and verify task quality.

## 3. What others do (research summary)

| Source | Technique | Relevance to us |
|---|---|---|
| [The Complexity Trap (JetBrains/TUM, NeurIPS 2025)](https://arxiv.org/abs/2508.21433v2) | **Observation masking.** Keep all reasoning and actions; replace tool outputs older than the last M=10 turns with a placeholder. It matched or beat LLM summarization at about half the cost. LLM summaries made trajectories **13–15% longer**, possibly by hiding failure signals. A hybrid (mask first, summarize late at about 30K) was cheapest, at −7% vs masking and −11% vs summary. | Strongest evidence. Use masking first and summary rarely, and measure turns per task, not just tokens. |
| [Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) | KV-cache hit rate is the top metric. Keep the prefix stable and append-only. Never add or remove tools mid-task (mask them instead). Use the file system as memory. Recite a todo near the end. Keep errors in context. Make compression **restorable** (keep the path or URL so content can be fetched again). Vary serialization to avoid self-few-shot ruts. | We already do exact replay. Compaction must be restorable and keep the latest failures. Check that the local tool list never changes mid-run. |
| [Anthropic: context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), [context management](https://www.anthropic.com/news/context-management) | Tool-result clearing is "the safest lightest touch" form of compaction. Tune compaction prompts for recall first, then precision. Use structured notes, and sub-agents that return 1–2K token digests. Load context just in time (identifiers plus tools). | Same ordering: clear results before summarizing. |
| [Factory: evaluating compression](https://factory.ai/news/evaluating-compression) | **Anchored iterative summarization.** Persistent sections (intent, file changes, decisions, next steps) are merged on each compaction rather than regenerated. It beat the OpenAI and Anthropic built-ins in their evaluation. Optimize tokens per task, not tokens per request. | Our `Handoff` schema is close; it should merge into the previous handoff. |
| [OpenHands condenser](https://docs.openhands.dev/sdk/arch/condenser) | Keep the first k events and a recent tail; summarize the middle once an event count is exceeded. The summarizer LLM is separate from the agent. | Same shape as our lab. Its trigger is a threshold, which we are replacing. |
| Codex CLI ([community notes](https://gist.github.com/badlogic/cd2ef65b0697c4dbe2d13fbecb0a0a5f)) | Auto-compacts once a token limit is crossed. Users report the model "goes off the rails" when this happens mid-task. | Supports the user's idea of compacting at task checkpoints, not mid-step. |
| [Chroma: context rot](https://trychroma.com/research/context-rot) | Reliability drops as input grows, even on simple tasks, across 18 models including Qwen3. | A smaller working context also helps quality, not just speed. |
| [SWE-agent ACI](https://arxiv.org/abs/2405.15793v2) | Interface design drives results: windowed file viewer, filtered test output, capped search. | Tool outputs should be token-efficient at the source. |
| [Qwen3.6 card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B) | Thinking/general: T=1.0, presence 1.5. **Thinking/precise coding: T=0.6, presence 0.0.** `preserve_thinking` is trained for agent use. | The A3B native profile uses the *general* preset (1.0 / 1.5). A presence penalty on code penalizes repeated identifiers. An A/B test is needed. |

## 4. Proposed design: budgeted checkpoint compaction

Principles: compaction is code-owned and deterministic. It happens only at checkpoints, as one
prefix break per compaction, compacts well below the budget so it rarely recurs, is restorable,
and never touches authority.

1. **Budget.** `W` is the working budget in prompt tokens, set per engine from the
   decode-vs-depth curve; a placeholder is 16–24K on current Ollama. `C` is the hard ceiling of
   32K. After compacting, the target is about `0.5 × W`.
2. **Checkpoints** (deterministic runtime events, never model-chosen):
   - after a verifier or `check_project` result;
   - after an edit whose follow-up check completed;
   - on a phase change (explore → edit → verify);
   - after a repair attempt is rejected.

   At each checkpoint the runtime projects the next cycle:
   `current + max_tool_result + max_output`. If that would exceed `W`, it compacts now.
   Otherwise it does nothing. As a backstop, if a turn would exceed `C` between checkpoints, the
   runtime compacts at the next tool-result boundary and records a `budget_overrun` event, to
   be investigated.
3. **Tier 1: restorable masking.** This is free, needs no model, and is tried first. Tool
   observations older than the last M turns become a stub:
   `[read_file app/x.py sha256=… 4,812 bytes — omitted; re-read if needed]`. Assistant
   actions, tool calls, all errors, verifier failures and protocol corrections are kept. Old
   thinking is dropped at the boundary; exact replay resumes after it.
4. **Tier 2: anchored handoff.** Used only if tier 1 cannot reach the target. The existing
   strict `Handoff` schema runs on Gemini Flash-Lite, or locally. It is merged into the previous
   handoff (anchored iterative) rather than regenerated, and cites message IDs. It replaces the
   masked middle.
5. **Always preserved verbatim:**
   - the system prompt;
   - the original task;
   - code-owned runtime-policy and authorized-human items;
   - the latest verifier failure;
   - the current file hashes;
   - the last complete action/result pair;
   - a code-owned progress/todo recitation at the end.
6. **Trust (AGENTS.md rule 16).** Handoff text is MODEL_INFERENCE. The lab currently injects it
   as a `user` message, which is the authorized-human channel. It must arrive in a channel that
   cannot be mistaken for human or runtime authority. Options: a runtime-owned tool result
   carrying its trust label, or a labeled block inside the runtime's context packet.
7. **Cost accounting.** Each compaction costs one re-prefill of the compacted prompt (for
   example, about 10K tokens at about 470 tok/s, roughly 20 s on Ollama). That pays off once the
   faster decode saves more than this before the next compaction. Track tokens per task, turns
   per task, wall time and pass/fail, not just per-request size.

## 5. Experiments, in order (the GPU is exclusive; nothing runs alongside the matrix)

1. **Decode vs depth × allocation × KV type** (about 30 min, no model choices involved).
   - Replay captured requests truncated to about 8K, 16K, 24K and 32K prompt tokens, with a
     fixed output of 256 tokens.
   - Ollama: `num_ctx` {32768, 131072} × KV {q8_0, f16}, MTP on, plus one MTP-off column.
   - MLX: the same depths.

   This answers the 32K-allocation question, finds the cause of the 3× slowdown, and sets `W`
   per engine.
2. **Sampling A/B on captured requests.** General (1.0 / 1.5) versus precise-coding
   (0.6 / 0.0). Measure output length, tool choice and edit exactness.
3. **Implement the checkpoint compaction** (tiers 1 and 2) with offline tests for allowed and
   denied behavior. Build it in a git worktree: the running matrix pins `src/**/*.py` and will
   refuse its next trial if anything changes.
4. **Paired trials** under the user-authorized $2 cap. The runner reuses
   `run_compaction_comparison.py`'s waiting, lease and pinned-hash structure. Compare against the
   final matrix baseline on pass rate, wall time, turns, tokens per task, and minimum decode
   tok/s.

## 6. Audit notes on the earlier lab (`scripts/compaction_lab.py`)

- **Correct:** history is replaced in place (`history[:] = fresh` on the stored list); the class
  patch reaches `run_engine_comparison.child`; the schema is strict; evidence references are
  checked; on failure the full history is kept; the reservation is settled on every path.
- **Stall:** the runner polls forever when its baseline stops without `monitor-stop.json`. The
  tuned matrix was stopped that way, and `src/` changes would also have aborted it before launch.
- **Threshold trigger** (16K prompt tokens, 6-call cooldown) fires mid-step. Replace it with
  checkpoints (section 4).
- **Handoff is injected as `user` role.** This is a trust-channel issue (section 4.6).
- **Handoff is regenerated** from the full history each time instead of anchored and merged.
- **No masking tier.** Every compaction is a paid summary.
- **No tests.**

## 7. Other harness findings (measured on the live trial `rep1-ollama-cold`, call 22, 35.4K prompt tokens)

The prompt is 112K characters in all:

| part | characters | share |
|---|---|---|
| tool results | 73K | 65% |
| assistant thinking (exact replay) | 31K | 28% |
| system + task | 5K | 4% |
| tool schema (5.4K characters, identical hash on all 22 calls) | — | — |

The tool list never changes mid-run. Good.

1. **Verification output is noisy and repeated.**
   - Every `edit_file` / `check_*` result carries the full pytest output, including a 2.5–4.3K-character
     `warnings summary` (Starlette DeprecationWarnings). That is about 16.7K characters (about 13% of the
     prompt) of pure noise.
   - The same failing test is restated in full after each of 4 edits.
   - Newlines inside stdout are escaped as `\x0a`, 40–66 per result, which tokenizes far worse
     than real newlines.

   Fixes: filter warnings at the source (`-p no:warnings` / `-W ignore::DeprecationWarning`, or strip
   the section), keep real newlines, and when the failure set is unchanged, emit
   `unchanged since <sha>: test_upload_idempotent_and_conflict (assert 422 == 409)` instead of the
   full trace. The runtime's own fingerprint must be the source of the "unchanged" claim; the
   model's is not.
2. **`read_file` pages at about 3K characters (about 80 lines).** Twelve reads covered 8 files. Four of them were
   page continuations (storage p2, server p2/p3, pdf_documents p2). Each extra call costs a
   whole thinking turn (10–200 s here), which is far more than the bytes saved. Fix: return
   whole files up to a byte cap (about 12–16K characters) and page only past it.
3. **Calls are the expensive unit, not bytes.** Decode dominates (prefill is 1–5 s per call).
   Reducing the number of calls (larger reads, a combined "edit + run the failing test" path, which
   already happens implicitly) is worth more than shaving tokens.
4. **Old thinking is 28% of the context.** It is kept on purpose for prefix stability and
   `preserve_thinking`. Dropping it at a checkpoint (section 4, tier 1) is the single largest
   reclaim.
5. **Sampling preset** (section 3): the A3B profile uses the general thinking preset, not the
   precise-coding one.
