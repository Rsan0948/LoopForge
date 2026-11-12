# Model performance audit — 2026-10-12

Audit and recommendations only. No application code, model settings, dependencies, or campaign state changed; no inference or new qualification run. Brook remains the intended campaign; HelicOps stays parked. One repository and one model agent at a time.

The best next investment is increasing verified useful work per model call. Keep A3B as the baseline, repair the task/evidence/feedback contracts, then qualify inference profiles. A backend replacement may help, but cannot repair an impossible reporting obligation or missing implementation evidence.

## Evidence and limits

This audit combines current source inspection, read-only Ollama metadata, existing benchmark artifacts, the earlier [full-surface audit](campaign-full-surface-audit-2026-10-12.md), and primary-source research. Earlier reproduced findings are identified by that report's F numbers; they were not re-run here. Source on disk is not proof the service has loaded it. Recommendations below are not measured improvements.

Observed setup: Apple M1 Max, 64 GiB unified memory; Ollama 0.32.15; `qwen3.6:35b-a3b`, GGUF Q4_K_M, family `qwen35moe`, reported 35.5B parameters. Parent alias: `qwen3.6-source:35b-a3b-mtp-q4_K_M-20260824`. `/api/ps` showed no loaded model during this audit. Metadata declares tools/thinking capabilities. It does not prove the exact multi-turn reasoning protocol works.

Saved [model comparison](../../.loopforge/campaign/monitor/performance/model-comparison.json):

| A3B case | Wall time | Prompt processing | Decode rate |
|---|---:|---:|---:|
| Short, with model load | 29.21 s | 0.28 s | 48.06 tokens/s |
| Review, cold prompt | 16.07 s | 11.18 s | 29.93 tokens/s |
| Review, appended follow-up | 1.59 s | 0.50 s | 29.14 tokens/s |
| Review, shifted prompt | 14.73 s | 10.57 s | 31.30 tokens/s |

The short case included 23.60 seconds of loading. The warm append case supports preserving stable prefixes; these different prompts/output lengths are not a controlled cache speedup measurement. The script used non-thinking, short outputs and HelicOps excerpts: it does not establish Brook repair quality or thinking-mode throughput. Some outputs hit their output cap. Do not compare these results directly with long coding attempts or claim a campaign speedup from them.

## Highest-value changes

### 1. Make every task solvable and finishable before spending inference

**Evidence:** F01 admits mandatory evidence that cannot fit the final report. F03 can assign documentation drift with documentation alone. F07's generic traversal revisits headers and provides weak coverage of meaningful behavior.

Create a code-owned task packet with one behavior/claim, exact base revision, likely implementation and caller, relevant tests, allowed edits, immutable checks, and an explicit success condition. Documentation work needs a claim-to-implementation pair and a way to report uncertainty. Discovery may propose a task, but admission must validate the packet. Preflight check dependencies, evidence accessibility and exact final report capacity before reserving an attempt.

Use a coverage ledger keyed by revision, behavior/claim, and inspected source interval. Prefer changed, unverified or failure-prone behavior. Reading a new header is not completion; unchanged evidence need not be rediscovered in every lane. Avoid decomposing tightly coupled changes so aggressively that the task loses the causal context needed to fix them.

**Acceptance:** no admitted task fails solely because its mandatory evidence cannot be represented; doc-drift fixtures require implementation evidence; reviewed coverage and unresolved areas are explicit.

### 2. Give the model better source navigation and exact evidence

**Evidence:** coding offers literal search and exact-substring editing, but no symbol/reference navigation. F06 identifies unreachable long-line tails and empty-file obligations. Tiny excerpts increase navigation turns and can omit the relevant control flow.

Add a compact, task-ranked repository map, symbol outlines and bounded definition/reference lookup. Return exact source with path, revision/blob hash, range, truncation status and a usable continuation cursor. Summaries guide navigation; they cannot substitute for cited source. Resolve a symbol to its body and nearby relevant callers without dumping an entire file. Index by revision and invalidate affected entries after edits. Retain literal search for config, prose and unsupported languages.

Keep the tool set small. A bounded evidence-bundle read could fetch several independently selected ranges in one action while still executing sequentially. An atomic, expected-file-hash-checked multi-edit operation could avoid invalid intermediate patches and repeated checks. Both require code-owned limits and all-or-nothing validation; neither is permission to enable arbitrary commands.

**Acceptance:** long lines, empty files and changed indexes remain navigable; fewer read calls on held-out tasks without lower evidence accuracy or broader authority.

### 3. Preserve useful working memory across attempts

**Evidence:** `campaign_coding.py` provides review feedback and progress records already. F08 shows remaining tail slicing and incomplete failure details. `ollama_model.py:_conversation` has a character-limit trimming path without the explicit compaction notice used by `_compact_history`.

Maintain a bounded structured record: current hypothesis, inspected source IDs, exact patch identity, command/environment identity, failure category and concise output, rejected approaches, unresolved question, next discriminating check. Keep whole records and on-demand links to full artifacts. Distinguish source facts, test observations and model hypotheses. Invalidate claims whose evidence changed.

Unify compaction so every path preserves current blockers, latest verifier result and current task state. Preserve an index of evicted evidence for rereading. A retry should receive the actual failure and a reason to pursue a different approach, not just the last characters of a report. Keep the existing immutable attempt caps. A promising prepared patch can only use an already-authorized verification path; exhausted tasks cannot be silently recreated.

**Acceptance:** after forced compaction or retry, the model can identify what failed, what was tested and what remains; it does not repeat the same rejected patch without new evidence.

### 4. Separate model decisions from mechanical verification

**Evidence:** coding verifies writes through `RepairVerifier`; explicit check actions can be followed by another verification of the same patch. Current no-progress guards are useful, but reading new material alone does not establish semantic progress.

Use sequential stages: inspect → reproduce/characterize → edit → deterministic checks → fresh evidence review. Reuse LoopForge's event/reducer and verification contracts. Let the model decide the diagnosis and edit; let code schedule fixed checks, record receipts and enforce completion. Run cheap structural checks immediately, then the required suite at a coherent patch boundary. Retain all acceptance checks at final verification.

Reuse a check result only when the full workspace/input identity, command, image, environment and dependency identity match and the check is suitable for reuse. Never cache nondeterministic or externally dependent results as definitive acceptance. Classify environment failures separately from product failures so the model does not rewrite correct code to compensate for a broken runner.

Fresh review should receive the claimed behavior, patch and test evidence, but not be forced to endorse the coder's explanation. Multiple review angles remain sequential. Avoid repetitive critique rounds without new evidence.

### 5. Correct and explicitly version the inference profile

**New source/metadata finding:** the adapter defaults to `temperature=0.0`; campaign wiring does not override it. The model alias supplies `presence_penalty=1.5`, `top_p=0.95`, `top_k=20`, `min_p=0`, `repeat_penalty=1`, and `draft_num_predict=2`. The request overrides temperature while leaving other defaults implicit. This is a mixed profile, not a deliberately qualified coding profile.

Qwen's official precise-coding thinking profile specifies temperature 0.6, top_p 0.95, top_k 20, min_p 0, presence penalty 0 and repetition penalty 1. Its non-thinking profile differs. Treat these as comparison candidates, not guaranteed improvements. The model also supports thinking preservation; local serving/template compatibility must be verified. Do not copy the card's large output allowances into every tool turn. [Qwen model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)

Version all effective parameters, model digest, tokenizer/template identity and server version. Trial a precise-coding profile against the existing baseline. Keep reporting as a separately qualified constrained-output profile; a creative sampling profile need not help evidence serialization. Do not turn on broad repetition penalties merely to compensate for a harness loop.

**New protocol finding:** `_ResponseMessage` has no `thinking` field and ignores extras. `_parse_action` reconstructs the assistant history with empty content and the tool call, discarding returned assistant content as well. Thus the harness cannot preserve the model's returned reasoning between successful tool turns. This is confirmed information loss; its quality cost is unmeasured. Ollama's examples append the returned assistant message before the tool result. [Ollama tool protocol](https://docs.ollama.com/capabilities/tool-calling)

Test validated, bounded preservation in the provider adapter with the exact local template. Keep reasoning as model inference, never runtime authority or verified facts. Avoid replaying malformed actions. Compare task success, token overhead and repeated reasoning against the present behavior. Reasoning retention and compact factual memory serve different purposes.

Current coding permits 8,192 output tokens and switches off thinking for the rest of a run after certain exhausted no-action responses. Record this mode change explicitly. Prefer prequalified phase profiles and smaller task decomposition; repeatedly increasing output limits can turn one tool selection into minutes of generation. Changes must remain within immutable run limits.

### 6. Use the context window accurately

**Evidence:** coding uses 32,768 context tokens, reserves 8,192 output plus 1,024, then compares serialized UTF-8 request bytes with the remainder. The context builder separately uses a character heuristic. The resulting 23,552-byte request ceiling is conservative, not actual token accounting; tool schemas, escaping and code syntax affect it. Reporting adds its separate 26,000-byte bound.

Implement an adapter-side counter aligned with the actual tokenizer and rendered chat/tool template, or calibrate a conservative bound against provider measurements if exact rendering is unavailable. Keep a safety reserve and fail explicitly rather than silently dropping required evidence. Reuse the core token-counter port, not provider code in the domain.

First test better selection at 32K; then compare 64K only on tasks that demonstrably need it. A larger window can increase memory/prefill costs and distract the model. Keep policy/tool prefixes stable and append current observations. Rebuild/compact deliberately instead of rewriting the entire prefix every turn.

### 7. Measure accepted work and qualify changes honestly

**Evidence:** F15's current qualification grader can accept a wrong claim with the expected file paths. F13's adapter drops timing telemetry. These prevent trustworthy optimization.

Build a small frozen Brook task suite covering real bugs, doc contradictions, negative controls, cross-file fixes, ambiguous evidence, tool errors, long lines, context pressure and environment failures. Run the real planner for discovery cases. Grade actual contradictions and behavior, not matching filenames or a model's confidence. Repair tasks need a regression that fails on the base and passes on the candidate, plus unchanged checks; manually adjudicate an initial sample and disputed findings. Keep development and held-out tasks separate.

Capture per-stage load, prompt processing, decode, tool, verification and idle time; tokens; malformed actions; repeated reads; context eviction; repeated patches; first-attempt success; verified findings; false positives; and accepted repairs/hour. Ollama exposes timing and token counters; retain supported fields and mark unsupported metrics unknown. [Ollama chat API](https://docs.ollama.com/api/chat)

Compare one change at a time, with repeated trials for stochastic profiles and uncertainty reported. Pin versions and quantization. Do not publish a speedup claim until quality holds on held-out work. Automatic controls remain evaluated with deterministic allow/deny tests regardless of model scores.

## Reuse shortlist

| Component | What to reuse | Recommendation and integration boundary |
|---|---|---|
| Existing LoopForge context lifecycle | Preservation classes, provenance, selection/accounting, TokenCounterPort | Extend current integration; avoid a second unrelated compaction system. |
| Existing LoopForge repair/runtime | Immutable checks, tool metadata, event state, budgets, verification | Keep these as the execution authority; improve phase scheduling and evidence receipts. |
| Existing LoopForge policy heuristics | Evidence-derived bounded context/stall suggestions | Use after reliable evaluations exist. Current heuristics are suggestions, not proof of optimal settings. |
| Existing LoopForge routing | Compatibility checks and reason-coded decisions | Profile selection for the same approved A3B model is sufficient initially. Dollar-cost tiers are a poor standalone local-performance objective. Do not introduce model switching or parallel workers by default. |
| Aider repository map | Ranked symbols/dependency map within a token budget | Highest-priority outside concept to prototype. Port a narrow retrieval component or implement the approach behind a port; evaluate relevance on Brook. |
| Serena | Definition, reference and symbol outline lookup via language servers | Second-stage read-only integration if the map plus literal search is insufficient. Restrict workspace and tools; language-server subprocesses need an explicit capability contract. |
| Qwen-Agent | Model-specific tool protocol examples and compatibility behavior | Use as a reference/test oracle, not a replacement for LoopForge authority, state or approvals. |
| MLX-LM | Apple-silicon inference and prompt-cache facilities | Later backend pilot using the same held-out tasks and comparable quantization. No claim it is faster or fully compatible with this exact artifact yet. |

Aider documents a task-relevant ranked repository map, making it a strong match for our discovery problem. [Aider map documentation](https://aider.chat/docs/repomap.html) Serena provides symbol-level retrieval and references through language-server integrations. [Serena](https://github.com/oraios/serena) Qwen-Agent provides a model-oriented agent/tool framework useful for protocol comparison. [Qwen-Agent](https://github.com/QwenLM/Qwen-Agent) MLX-LM provides local inference and prompt caching, with memory/quality tradeoffs for cache settings. [MLX-LM](https://github.com/ml-explore/mlx-lm)

Before copying or vendoring any implementation, pin a revision, review its license and transitive dependencies, and test the narrow adapter. This was a documentation-level shortlist, not a source/security audit of those projects. Avoid importing an entire orchestration stack to obtain one retrieval feature.

## Setup optimization after the quality blockers

Keep one shared model lease across campaign and Hub clients. Preserve residency during authorized contiguous work to avoid load churn, with a bounded idle policy. Measure GPU residency, memory pressure and swap during representative runs; 64 GiB installed memory does not imply all of it is free for inference.

The local alias already declares speculative drafting (`draft_num_predict=2`). Do not claim speculation is missing or working from that setting alone: verify backend support and benchmark on/off behavior later. Likewise qualify cache quantization or a higher-weight quantization only after obtaining a representative quality baseline. More precision can cost memory/bandwidth; a faster configuration can cost correctness.

Keep n8n responsible for durable scheduling and visibility rather than the inner reasoning loop. Continue bounded batches promptly when authorized; reserve the 15-minute monitor for supervision. Reducing scheduler idle and duplicate archival work improves campaign throughput without changing model intelligence.

The research supports concise, unambiguous tools, realistic evaluations, targeted retrieval and structured durable context. These are principles to test locally, not transferable benchmark guarantees. [Tool design guidance](https://www.anthropic.com/engineering/writing-tools-for-agents), [context guidance](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

## Recommended implementation order and gates

1. Fix task/report finishability, evidence pairing, source-reader gaps and the qualification grader. Add complete effective-profile and timing records.
2. Add repository navigation and structured failure memory; consolidate compaction. Establish a measured baseline on solvable tasks.
3. Compare explicit coding sampling, bounded reasoning preservation, and phase-specific context/output profiles. Keep only changes that improve verified outcomes or reduce time without a quality regression.
4. Remove redundant checks and scheduling idle with identity-safe receipts and immediate authorized continuation.
5. Evaluate MLX-LM, quantization and drafting/cache options after harness quality is stable.

Restart remains gated by the prior audit's blocking fixes and the user's hold. A canary must complete discovery, repair, immutable checks, fresh review and accessible artifact delivery. Rejected findings must remain rejected, exhausted attempts must stay exhausted, and no stage may expand authority. This audit adds recommendations; it does not certify those gates passed or authorize a restart.
