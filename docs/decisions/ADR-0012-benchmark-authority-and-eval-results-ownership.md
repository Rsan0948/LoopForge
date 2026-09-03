# ADR-0012 — Benchmarks as operator-owned authority, eval reports as operator-owned artifacts

Status: Accepted

## Context

PACS-016 (locked benchmark and multi-trial evaluation laboratory) evaluates
runtime policy scientifically. That purpose collapses if the thing being
measured can move the measuring stick. Three pressures shaped the decision:

1. **Gaming pressure.** An adaptive execution policy optimizes within its
   granted authority (AGENTS.md rule 12). If benchmark tasks, graders, or
   fixtures were tunable by runtime configuration — or worse, by anything the
   model can influence — every reported improvement would be suspect. The
   false-success metric, the laboratory's headline number, is exactly the
   metric a gamed harness would report as zero.
2. **Divergence pressure.** "Same benchmark" claims require the benchmark to
   be one thing. A fixture edited casually between two eval runs makes their
   reports incommensurable while looking identical.
3. **Write-boundary pressure.** Eval reports feed operator decisions (and,
   in PACS-017, shadow-policy comparisons). If a run under test could write,
   rewrite, or delete its own evaluation results, the results would be
   self-assessment, not evidence.

## Decision

1. **Benchmark definitions are code-owned and content-locked.**
   `domain/benchmarks.py` owns the closed vocabulary (12 categories, grader
   ids, verdicts); `workloads/benchmarks.py` owns the fixture set.
   `benchmark_content_lock()` hashes every semantic byte — fixture files,
   solutions, commands, acceptance/patch-constraint fields, approval/fault
   wiring, and the *source* of acceptance hooks — and a test pins the literal
   hash, so any benchmark drift fails loudly and deliberately. `BenchmarkSuite`
   refuses to exist with a mismatched hash.
2. **Graders are deterministic operator authority, fed only durable
   evidence.** Graders are pure functions over the task spec, the
   authoritative event stream, and operator-collected workspace evidence
   (symlink-safe, byte-capped). Model output reaches them only as recorded
   evidence; required check names derive from code-owned hook definitions,
   never from strings a workload can influence; hook evidence counts only
   from passing verification summaries. A false success — verifier-granted
   success any grader rejects — is a first-class verdict, not an
   afterthought.
3. **Eval reports are operator-owned artifacts; the serving plane only
   reads.** `EvalReportStore` writes versioned JSON under an operator-owned
   results directory with atomic renames; loading revalidates through the
   domain constructors, so a tampered report file fails loudly. REST exposes
   reports and the locked suite definition read-only (no POST/DELETE); the
   console badges reports whose lock hash is stale against the live suite.
   Nothing inside a run under test can write a report.
4. **The comparison axis is runtime configurations, not model brands.**
   `EvalConfiguration` carries only code-owned knobs (budgets, no-progress
   limit, verification cadence, router flag), each narrowed against
   code-owned envelopes — a configuration can tighten authority, never widen
   it (rule 12). Fault injection (transient API, provider outage) is a
   code-owned, deterministic `ModelPort` decorator, fresh per trial.
5. **Live-model evals never enter deterministic CI.** Multi-trial live runs
   live in `tests/live/` behind Ollama/Docker skip probes; deterministic
   suites exercise the entire laboratory with scripted and fault-wrapped
   fake adapters.

## Consequences

Positive:

- benchmark changes are always explicit, diff-visible, and pinned — two
  reports with the same lock hash measured the same thing;
- the false-success metric is structurally hard to game: success requires
  the verifier and every independent grader to agree on durable evidence;
- reports are tamper-evident (domain revalidation on load) and staleness is
  operator-visible (lock badge in the console);
- the laboratory compares policies without ever granting them authority
  over their own evaluation.

Negative:

- the lock is intentionally brittle: any fixture edit, however cosmetic,
  forces a deliberate re-pin of the hash literal (friction by design);
- the content lock pins fixture/hook bytes but not grader or runtime policy
  code — those are operator authority pinned by their test suites, and a
  change there is visible only through git history, not the hash (recorded
  honestly in the workloads module docstring);
- read-only REST exposure means report curation (retiring obsolete reports)
  is a filesystem operation, not an API call — acceptable for an
  operator-owned artifact store.
