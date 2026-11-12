# What we are building, and what current research says to do — 2026-10-24

The user asked for this before the architecture work starts. Section 1 proposes a definition for
the user to confirm. Section 2 compares LoopForge with current research and practice: what we
already do, and what we are missing or contradicting. Section 3 explains how the answers change
the architecture plan in `architecture-audit-2026-10-24.md`.

Most 2026 sources below are arXiv preprints with self-reported numbers. They are directional
evidence, not settled results.

## Decisions (user, 2026-10-24)

- **Definition confirmed.** B is built on A.
- **Split.** A stays the pure open-source core. B becomes a separate app that depends on A's
  source as an ordinary dependency (no middleware). General improvements go upstream into A. See
  [ADR-0021](../decisions/ADR-0021-core-and-agent-product-split.md). For now the split is
  documentation; the physical move comes later.
- **Scope.** Repair and feature work are B's priorities. From-scratch builds are dropped. R1's
  ledger still applies, because feature work has the same "progress inside a probe" problem.
  R9 and build-only items drop.
- **Positioning.** Open-source positioning matters for A, not for B.

## 1. Definition

The repository holds two things that the history has blurred together.

**A. The foundation (complete, September 2026).** A reference architecture with one rule: *models
propose, a deterministic runtime decides.* The runtime owns authority, state, verification,
budgets, retries and termination. Runs are event-sourced, replayable and governable. This is the
"PACS-001…017" work and the open-source `loopforge-console` package.

**B. The product (in progress since 2026-10-20).** An unattended repository agent built on A:

> Give LoopForge a repository and a goal (fix a bug, add a feature, or build an app) and leave
> it. It returns a candidate that has been verified independently and reviewed, but not merged,
> plus an honest report. A frontier model (Gemini) supervises: it investigates, plans, assigns,
> judges and recovers. A local model (Qwen 3.6 35B-A3B on MLX) does the coding at no per-token
> cost. Authority, budgets and the acceptance checks are fixed before the run starts. It asks the
> user only consequential questions, keeps working while a question waits, and branches into an
> experiment when a question times out.

**Success criteria** (proposed; none is measured today):

1. **No false deliveries.** Nothing is ever reported as done unless the runtime has verified it.
2. **Delivered rate per goal**, measured over repeated runs (pass^k, §2 R8), not one lucky run,
   for each task class: repair, feature, from-scratch build.
3. **Cost per delivered goal**: Gemini spend, bounded by the intake.
4. **Wall time per delivered goal.**
5. **Interventions per goal.** The target is zero.

**Non-goals for now:**

- Parallel writers.
- Remote push, merge or deployment.
- The Brook/HeliCops review-campaign product.
- Benchmarks against other agent products.
- Self-modifying harnesses.

**Users and environment.** One operator, on one Apple Silicon machine, running local inference.
The packaged console is the eventual distribution.

**What contradicts this definition today** (from the architecture audit):

- The product is hard-coded to four benchmark projects.
- Two products share one coordinator.
- The path validated in trials (`launch`) is not the path the console uses.

**Questions only the user can answer:**

1. Is B, built on A, the right statement?
2. Which task class matters most?
3. Does the open-source reference-architecture positioning still matter, or is this now a
   personal tool?

## 2. Research compared with LoopForge

### Already aligned (keep)

| Practice | Evidence | LoopForge |
|---|---|---|
| A deterministic layer decides; the model only proposes | Deterministic control planes and authorization boundaries for coding agents (arXiv 2606.26924, 2601.17744) | The core thesis; code-owned tool authority |
| Heterogeneous teams: a strong planner with a cheap executor | AgentCARD: mixed teams sit on the cost-accuracy frontier, matching the best homogeneous team at up to 12× lower cost (arXiv 2606.20629) | Gemini supervises, local Qwen codes |
| Delivery must carry evidence | Evidence-carrying termination: 0/66 unsupported completions vs 40/66 for a model-judged controller (arXiv 2608.23623) | Delivery requires a runtime-verified receipt and review |
| Context management under tight windows; elision before summarisation | Harness ablation: management matters most at small windows; staged elision then summary is the most efficient tier; making elided content recallable adds no accuracy (arXiv 2609.20804) | Tier 1 masking, then Tier 2 Gemini handoff |
| Test like a user, end to end | Anthropic's long-running harness: browser automation found bugs that unit tests and curl missed | Frozen Chromium probes |
| Predefined tools for weaker models | Harness ablation: +15 points for a 30B model with predefined tools | Confined tool set |
| One writer; parallelism only for reading | Cognition and Anthropic positions, reconciled: parallelise reads, not writes to shared state | One worker |

### Missing or contradicted, by expected impact

**R1. Progress as a durable, fine-grained ledger, built one feature at a time.** *(Highest impact,
and it reshapes the gate design.)*

- **What the research says.** Anthropic's harness for multi-session builds uses:
  - an initializer that expands the goal into a JSON feature list, each feature with test steps
    and `passes: false`
  - an `init.sh` and a progress file
  - an initial git commit
  - coding sessions that each read git log, progress file and feature list, run a smoke test,
    implement **one** feature, test it end to end, commit, and update the notes

  The feature list exists because agents otherwise "declare victory" or try to do everything at
  once. NL2Repo-Bench (ICML 2026) names premature termination, loss of global coherence and weak
  planning as the dominant long-horizon failures in from-scratch builds.
- **What we do.**
  - One large objective string.
  - Three acceptance probes, each worth all or nothing.
  - The worker runs as one long chat that gets compacted.
  - "Progress" is a composite score that cannot move inside a probe (audit F2).
  - There is no commit per step, and no resume from artifacts.
- **Adopt, keeping our thesis.** Make the acceptance contract a **code-owned feature ledger**:
  - Each probe assertion, or each requirement with its own check, becomes a ledger item.
  - The *harness*, not the model, flips `passes` from probe results. That is stronger than
    Anthropic's version, where the model marks items done.
  - After each new passing item, commit a checkpoint and write a progress note.
  - A new attempt resumes from the best checkpoint plus the ledger, not from chat history.
  - The worker's assignment becomes "make the next failing item pass".
- **What this fixes or replaces.**
  - The score hack and construction credit (F2).
  - Most of the no-progress machinery: progress becomes the count of passing items.
  - The supervisor's blindness to *why* checks fail (F4): the ledger names the failing items.
  - The from-scratch failure mode, in which the whole app has to come into existence before any
    signal moves.

**R2. Stop rules should prevent waste, not end progress.**

- **What the research says.**
  - The ablation harness stops only after *eight consecutive identical failing calls*.
  - Practitioner designs nudge before accepting termination, and on a detected loop they make one
    final no-tools summary turn rather than killing the run.
  - Premature stopping, not runaway cost, is the documented failure.
- **What we do.** About 30 limits, several based on scores or evidence counters, cause premature
  termination (audit F1–F3, F6).
- **Adopt three kinds of stop:**
  1. **Hard budgets:** time, money, iterations and work tokens.
  2. **Literal loop detection:** the same action producing the same result N times.
  3. **Stuck signals that trigger recovery:** replan, a summary handoff, or a return to the
     supervisor with the ledger, instead of termination.

  This is the shape the single `domain/gates.py` from the architecture audit should take.

**R3. Give the model the context it was trained for.**

- **What the research says.**
  - Qwen reports SWE-bench Verified 73.4 for this exact model, using a bash plus file-edit
    scaffold, a 200K window, and temperature 1.0 with top_p 0.95.
  - The ablation shows the benefit of context management falling from 35.7 points at 32K to 2.7
    at 128K, while overflow failures disappear.
- **What we do.**
  - We hold a 32–48K working budget.
  - Each compaction costs a 40–50 s re-prefill and drops state; there were two in a single Snake
    attempt.
- **Adopt.** One measurement, not a benchmark campaign: the same Snake attempt replayed at 48K,
  96K and 128K working budgets on MLX, comparing compactions, decode speed and outcome. Our depth
  data (41 → 37 tok/s from 40K to 57K) suggests the cost is modest. R1 also shrinks the context
  each attempt needs.

**R4. Use free local compute for parallel attempts, selected by our own verifier.**

- **What the research says.**
  - On Qwen 3.6-35B-A3B, SWE-bench Verified goes from 41.7% for a single attempt to 50.6% for the
    oracle choice among 4 attempts. Text consensus recovers about +3.3 points (Risa, arXiv
    2608.22191).
  - Verifier-based best-of-K reaches 55 → 68 at K=8 (arXiv 2602.03419).
  - Getting the headroom "requires a reliable selector".
- **Our position.** We already have the reliable selector: frozen, execution-based probes.
  Worker tokens are free.
- **Adopt, once R1 lands.**
  - Failed attempts restart from the **best** verified checkpoint, not the latest.
  - With idle time, for example overnight, run K independent attempts and keep the one with the
    most ledger items passing.
  - Bounded by memory: MLX holds about 20 GB per instance, so the attempts run one after another.

**R5. Show the supervisor full traces, not lossy summaries.**

- **What the research says.** Cognition: "share full agent traces, not just individual messages."
  Conflicting implicit decisions come from lost context.
- **What we do.** The supervisor gets a compacted summary that drops the failing lines (audit F4).
  That caused 19 paid inspections.
- **Adopt.** A structured attempt digest. Contents:
  - the ledger diff
  - each action with its result fingerprint
  - the failing items with their probe lines
  - the files changed

  It replaces both the free-text evidence and the diff, and it is bounded by structure, not by
  truncation.

**R6. Match the action interface to the model's training.**

- **What the research says.**
  - The ablation finds the best interface depends on the model; strong models do better with
    bash, weak ones with predefined tools.
  - Qwen's own SWE scaffold is bash plus a file editor.
  - Weak models fail often on calls the harness cannot execute.
- **What we do.** A strict, custom protocol, and every strictness has become a failure mode:
  - `sha256`-guarded edits
  - JSON-list multi-reads with a 4-file cap (F8)
  - exactly one call per turn (F9)
- **Adopt.**
  - Accept parallel calls and execute them in order, with read batches allowed.
  - Relax the argument protocols to Qwen's native tool-call style.
  - Measure the change on recorded trajectories (Phase A replay) before a live run.

**R7. Measure reliability, not anecdotes.**

- **What the research says.**
  - τ-bench introduced pass^k: GPT-4o has 61% pass@1 but 25% pass^8.
  - Computer-use agents: 78% pass@10 vs 36% for all ten runs succeeding.
  - Reliability degrades super-linearly with task duration (arXiv 2603.29231).
- **What we do.** Each fix is judged by one live run.
- **Adopt.** For each task class, k = 3 local runs per change, run free on MLX. Report pass^k and
  where each run stopped. This is the standing rule (test knock-on effects first) made
  measurable.

**R8. Gate edits on evidence for repair work.** *(Lower priority for builds.)*

- **What the research says.** ECLoop derives per-task conditions that must be observed before an
  edit or a submission. It reports +4.8 to +11.8 Pass@1 on SWE-bench Verified with fewer tokens
  (arXiv 2607.28815).
- **What we do.** We gate the *supervisor's* delegation on investigation, not the worker's edits.
- **Adopt.** Later, for repair goals.

**R9. Watch code quality across long builds.** *(Low priority.)*

- **What the research says.** SlopCodeBench: agent code is 2.3× more verbose and 2.0× more eroded
  than human code. Quality guidance lowers the starting level but not the rate of decay.
- **Adopt.** A structural-metrics check in review, once builds deliver.

**Considered and rejected:**

- **Self-evolving harnesses (Live-SWE-agent).** There is a capability floor: a small model
  degrades by 68% when it tries to self-evolve. It also conflicts with code-owned authority.
- **Tree search (SWE-Search MCTS).** R4 gets most of its value with far less machinery, because we
  already have an exact verifier.

## 3. How this changes the architecture plan

The architecture audit's step 3 ("one gate policy") should not just move today's 30 limits into
one file. It should implement R1 and R2:

1. The **feature ledger** becomes part of the frozen acceptance contract, scored by the harness.
2. **Progress is the ledger.** Checkpoints are git commits per newly passing item.
3. **Stops** are hard budgets, plus literal loop detection, plus stuck-means-recover.
4. The **attempt digest** (R5) is the only thing that crosses from worker to supervisor.

Revised order:

| Step | Change |
|---|---|
| 0 | Hygiene and CI |
| 1 | Composition root |
| 2 | Configuration records |
| **3** | **Ledger, checkpoints, gate policy and attempt digest** (R1, R2, R5) |
| 4 | Move workloads and stores |
| 5 | Typed errors and generated contracts |
| 6 | Retire legacy code |
| — | Measurements: R3 (context window), R6 (interface), R7 (pass^k); then R4 (best-of-K) |

R3, R6 and R7 run on the Phase A replay and smoke harness at no cost. R4 comes after R1, because
it selects using the ledger.

## Sources

- [Anthropic — Effective harnesses for long-running agents](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [An Empirical Study of Harness Design for Coding Agents (arXiv 2609.20804)](https://arxiv.org/html/2609.20804v1)
- [openJiuwen: Beyond Static Harnesses for Long-Horizon Coding Agents (arXiv 2608.27969)](https://arxiv.org/abs/2608.27969)
- [NL2Repo-Bench (ICML 2026, arXiv 2512.12730)](https://arxiv.org/pdf/2512.12730)
- [SlopCodeBench (arXiv 2603.24755)](https://arxiv.org/html/2603.24755)
- [When May an Agent Stop? Evidence-Carrying Termination (arXiv 2608.23623)](https://arxiv.org/html/2608.23623v1)
- [Preventing Premature Commitment in Coding Agents — ECLoop (arXiv 2607.28815)](https://arxiv.org/abs/2607.28815)
- [Specialize Roles, Mix Deployments — AgentCARD (arXiv 2606.20629)](https://arxiv.org/abs/2606.20629)
- [Disagree to Explore, Agree to Commit — Risa (arXiv 2608.22191)](https://arxiv.org/html/2608.22191)
- [SWE-World (arXiv 2602.03419)](https://arxiv.org/pdf/2602.03419)
- [LLM-as-a-Verifier (arXiv 2607.05391)](https://arxiv.org/pdf/2607.05391)
- [A Deterministic Control Plane for LLM Coding Agents (arXiv 2606.26924)](https://arxiv.org/abs/2606.26924)
- [Faramesh — Action Authorization Boundary (arXiv 2601.17744)](https://www.arxiv.org/pdf/2601.17744)
- [Building Effective AI Coding Agents for the Terminal — OPENDEV (arXiv 2603.05344)](https://arxiv.org/abs/2603.05344)
- [Live-SWE-agent (arXiv 2511.13646)](https://arxiv.org/abs/2511.13646)
- [Hierarchical Self-Improvement (arXiv 2608.08466)](https://arxiv.org/pdf/2608.08466)
- [SWE-Search (arXiv 2410.20285)](https://arxiv.org/pdf/2410.20285)
- [SWT-Bench (arXiv 2406.12952)](https://arxiv.org/pdf/2406.12952)
- [On the Reliability of Computer Use Agents (arXiv 2604.17849)](https://arxiv.org/pdf/2604.17849)
- [Reliability vs task duration (arXiv 2603.29231)](https://arxiv.org/html/2603.29231v1)
- [Qwen3.6-35B-A3B model card summary (benchmarks and harness footnotes)](https://www.labellerr.com/blog/qwen3-6-35b-a3b-open-source-ai-model/amp/)
- [SWE-bench Bash Only leaderboard (mini-SWE-agent)](https://www.swebench.com/bash-only.html)
- [LangChain — How and when to build multi-agent systems](https://blog.langchain.com/how-and-when-to-build-multi-agent-systems)
- [Cognition context-engineering principles (summary)](https://agentic-ai.readthedocs.io/en/latest/ContextEngineering/devin/)
