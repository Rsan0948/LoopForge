# Benchmark and evaluation methodology

How LoopForge evaluates runtime policy scientifically — the rules that keep
a reported number meaningful. Authority boundaries are pinned by ADR-0012
(benchmark authority, report ownership) and ADR-0014 (adaptive policies);
this document is the operator-facing method.

## 1. What is being compared

Reports compare **runtime configurations**, never model brands. A
configuration is a code-owned bundle of runtime settings; since PACS-017 it
may also carry a versioned **execution-policy reference** (`policy_id` +
`policy_version`) resolved per trial, fail-closed, against the built-in
registry — so `policy-baseline` vs `policy-adaptive-context` is a policy
comparison through the identical driver, graders, and fixtures.

## 2. The locked suite

- 12 repair categories, one fixture each: simple bug, multi-file,
  misleading failure, transient API, ambiguous success, context pollution,
  stale state, stall, prompt injection, HITL, parallel work, provider
  outage.
- `benchmark_content_lock()` hashes every semantic byte — fixture files,
  solutions, commands, acceptance/patch-constraint fields, approval/fault
  wiring, and the **source** of acceptance hooks. A test pins the literal
  hash; any benchmark drift fails the build loudly and deliberately.
- Every solvable fixture is pinned solvable end-to-end; every base is
  pinned failing. A fixture that cannot be solved by its own reference
  solution never ships.

## 3. Trials and the success algebra

- A **trial** is one run of one task under one configuration. Reports
  aggregate `trials × (config, task)` into `ConfigReport` rows.
- Success is **verifier-granted only**: the provider-independent verifier
  stack (commands + patch constraints + acceptance hooks) grants success;
  a model's own claim is evidence, never a verdict.
- **FALSE_SUCCESS is a first-class verdict**: verifier-granted success that
  any deterministic grader (verified-success, scope discipline, ground
  truth, recovery) rejects. The graders re-derive their verdicts from the
  authoritative event stream plus operator-collected workspace evidence,
  so a false success means verifier/grader *divergence* — a harness
  defect, surfaced loudly rather than recorded as model luck.
- The trial algebra is pinned by property tests: `successes +
  false_successes ≤ trials`, rates equal counts/trials, and the report
  constructors refuse anything else.

## 4. Report axes (schema v2)

Per `(config, task)`: trials, successes, false successes, success/false-
success rates, mean cost (USD), mean latency, mean total tokens, mean
human interventions, and the PACS-017 v2 axes — **mean context tokens
used**, **mean context items dropped**, **mean recovery events**. v1
artifacts load through a shim with honest zero-filled v2 means; tooling
that consumes the axes (the M8 derivation heuristics) excludes zero-filled
rows rather than diluting measured means, and fails closed when no
measured samples exist. A Pareto frontier across success/cost/latency/
human-intervention is computed over configurations.

## 5. Shadow and counterfactual evidence

- **Shadow runs** journal `ShadowDecisionRecorded` events (model route,
  context budget, verification cadence) for a candidate policy while the
  active policy executes. Shadow advice is never enacted; the active path
  is pinned byte-identical with and without a shadow. Shadow decisions
  are evidence for review and for the derivation heuristics — they carry
  no authority.
- **Counterfactual replay** re-drives a recorded run from a stream prefix
  with the recorded model/tool/verification turns (deterministic
  re-drive), reporting a closed outcome vocabulary (matched/diverged/
  incomplete). It answers "would this policy have changed this turn?"
  only where the prefix makes that decidable — never as a simulated
  model.

## 6. Deriving candidates from evidence

`policy --derive` reduces stored eval reports (plus shadowed context-budget
samples from a SQLite event store) through bounded, deterministic
heuristics: p90 context-token ceiling clamped into a code-owned envelope,
step-rounded floor, recovery-weighted stall threshold. The suggestion is
constructed through `ExecutionPolicy` domain validation (fail-closed),
registers as a CANDIDATE with its evidence basis, and is reviewed exactly
like a hand-authored candidate. Heuristics never promote, never apply,
and never feed a runtime directly.

## 7. Live validation protocol

Live-model evidence stays in `tests/live/` behind skip probes (Ollama
`devstral-small-2:latest` + Docker `python:3.12-alpine`), so deterministic
CI needs zero credentials and zero live infrastructure:

- **Matrix:** 3 repair-category tasks × 2 policy arms × 2 trials through
  the production `EvalTrialDriver`. Assertions are structural invariants
  only — terminal streams, full grader coverage, aggregation cross-check,
  hard zero-false-success — never a live-model success rate. The printed
  evidence table is the cycle record's self-documenting artifact.
- **Shadowed run:** one live container run with a wired candidate advisor;
  structural assertions on journaled shadow decisions and verifier-granted
  success.

Live outcomes are genuinely nondeterministic; the methodology records
distributions, not anecdotes, and never tunes the suite to a result.

## 8. What a report does not say

- A Pareto improvement under one live model does not generalize across
  models; configuration findings are runtime findings.
- Zero false successes is a harness-integrity invariant, not a claim that
  the verifier stack is complete; the suite covers 12 categories, not the
  world.
- Shadow agreement is not causation: a candidate that would have made the
  same decision provides no evidence it would do better elsewhere.
