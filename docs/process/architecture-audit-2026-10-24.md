# Architecture audit: integration, configuration, modularity, separation of concerns — 2026-10-24

Requested by the user before any of the bug fixes in `full-audit-2026-10-24.md`: "address
integration and fundamentals before chasing down those bugs". This audit asks one question: are
proper engineering design standards defined, and are they enforced across the codebase?

Evidence: code at `12967e8` on `feat/autonomous-supervisor-foundations`, plus the existing gates
(ruff, pyright, import-linter, the architecture test and CI), import-graph analysis, a
function-size scan, and the trial intakes under `.loopforge/tasks`.

## 0. Verdict

The **core is well engineered and enforced**:

- `domain`, `ports`, `application` and `workloads` follow the AGENTS.md layering.
- There is no `Any` in the core, and strict Pyright is clean.
- The run state is event-sourced, and both import contracts are kept.

The **product built on top of the core is not**:

- Everything added for campaigns, goals, supervision and engines since September lives in
  `entrypoints/`. That is 19K lines, 44% of the source.
- No gate constrains `entrypoints/`, so it has absorbed every role: domain policy, application
  services, persistence, tool adapters, verifiers, model decorators, prompts and configuration.

The bugs in the previous audit are symptoms of that:

- Stop rules live in four places.
- Composition is duplicated across several roots that differ from each other.
- Behaviour is switched by profile-name strings scattered across five files.
- Wiring happens by mutating objects after they are built.

Recommendation: first make the product layer follow the same standards as the core, in small
behaviour-preserving steps (§4). After that, most of the earlier fixes become one-place changes.

## 1. What is enforced today

| Standard | Enforced by | Status |
|---|---|---|
| Layer order: domain < ports < application < workloads < entrypoints | import-linter, `tests/architecture/test_dependency_dag.py` | Kept |
| Core packages never import adapters | Architecture test | Kept |
| Domain imports only the deterministic stdlib | Architecture test | Kept |
| Adapters never import entrypoints | import-linter | Kept |
| Strict typing | Pyright strict | 0 errors (Pyright prints an "update available" notice, which is harmless) |
| Lint | ruff, 28 rule families | Passes |
| Formatting | `ruff format --check`, run in CI | **Fails on 4 files** (`scripts/publish_goal_app.py`, `scripts/replay_presentation.py`, `scripts/run_engine_comparison.py`, `tests/unit/test_goal_launch.py`) |
| Coverage of at least 90% | pytest-cov in CI | Passes locally |
| CI on every change | GitHub Actions | **This branch has 48 commits (+41K lines) and has never been pushed, so CI has never run on it.** The last CI runs were Dependabot failures on Oct 1. |
| Lockfile matches `pyproject.toml` | Nothing | **It does not:** `websockets` was declared but missing from `uv.lock`. Running `uv run` during this audit synced it, so the working tree now has that 2-line change. |

**Not enforced anywhere:**

- Anything *inside* `entrypoints/`: what it may contain or import, cycles, and SQL use.
- Function and module size. Ruff's complexity rules are on, but 144 `noqa` suppressions bypass
  them, often with a copied reason ("explicit boundary dispatch/normalization" appears 19 times).
- Single composition, configuration ownership, and where stop rules may live.

## 2. Findings

### A. Layering and boundaries

**A1. `entrypoints/` is the de facto application.** It holds 47 modules. By role:

| Role | Classes defined in `entrypoints/` | Where they belong |
|---|---|---|
| Domain policy (stop and gate decisions) | `RequiredChecksAwareModel` (no-new-evidence, stalled-retry, check circuit), `CodingVerifier.blocked` and repetition, `PatchReviewVerifier` acceptance rule, the `writable` path policy | `domain` (pure policy over journaled facts) |
| Application services | `CodingCoordinator`, `CampaignHarness`, `GoalExecutionService`, `RepositoryInvestigation`, `RepositoryGoalWorker`, `CampaignGoalWorker` | `application` |
| Workload components (tools, verifiers, contexts) | `ConfinedCodingTools`, `ReviewTools`, `CodingVerifier`, `ReviewContext`, `CodingStateContext`, `RepairGuidanceTools`, `RepairDecisionTools` | `workloads/coding`, `workloads/review` |
| Model decorators with policy | `AssignmentGuidanceModel`, `DeadlineAwareModel`, `CompletionAwareModel`, `ReportingReviewModel`, `EscalatingRepairModel`, `RepairDecisionModel` | Application or workload, behind `ModelPort` |
| Persistence (raw SQL) | `CampaignStore`, `CodingStore`, `IssueStore`, the `goal_worker_operations` table created inline in `CampaignGoalWorker.__init__`, SQL in `campaign.py`, `cli.py`, `goal_review.py` and `supervisor.paid_budget` | `adapters`, behind ports |
| Configuration models | `CampaignConfig`, `CodingTaskConfig`, `RepositoryGoalConfig`, `ApprovedEnvironment`, `PreparedGoalConfig` | `config` package |
| Qualification and test tooling used in production | `RecordedSupervisor` (in `autonomy_qualification.py`, imported by `launch`) | `adapters` (as a recording decorator) |

The `workloads` layer exists for exactly this: `workloads/repair.py` holds the original repair
tools and verifier. The coding workload was built beside it in `entrypoints/` instead.

**A2. Import cycles inside entrypoints, hidden by function-level imports.**

- An 8-module cycle: `goal_investigation`, `repository_goal_models`, `autonomy_qualification`,
  `goal_launch`, `supervisor`, `goal_review`, `repository_supervisor`, `goal_runner`.
- A 4-module cycle: `cli`, `sessions`, `server`, `console`.
- 8 imports placed inside functions keep them from failing at import time.
- `campaign_models` is imported by 18 entrypoint modules. It is the true shared kernel of the
  product, and it lives in the outermost layer.

**A3. Production code depends on `scripts/` and on a gitignored lab directory.**

- The MLX "production engine" (`adapters/mlx_transport.py`) reads
  `Path(".loopforge/mlx-lab")/model.json` and a renderer binary built from
  `scripts/ollama_renderer`, relative to the current directory.
- The server it talks to is `scripts/mlx_server_agentic.py`.
- None of this ships in the wheel, and it breaks when started from another directory.
- Four test modules import from `scripts/`.

### B. Separation of concerns

**B1. Stop authority is split across four layers, and part of it is invisible to replay.**

| Where | Rules | State it uses |
|---|---|---|
| `domain.policy.ControlPolicy` | Budget, tokens, time, no-progress, iterations | Journaled |
| `application.runtime` | Tool circuits, model-failure streak | Journaled, except the streak, which is in-memory per drive |
| Entrypoint model wrappers (`RequiredChecksAwareModel`, `DeadlineAwareModel`) | No-new-evidence (8), check-circuit stop, check blocked (3 or inconclusive), stalled retry, deadline | **In-memory**: `self.progress`, `verifier.blocked`, `verifier.last_failure`. These stop a run by raising `ModelTurnError(PERMANENT)`. |
| Goal coordinator and reducer | Decisions, failures, rejections, research, stagnation, versions | Journaled |

The wrapper rules make authoritative stop decisions from state that is not in the event stream.
They do this by pretending the *model* failed. Replay cannot explain why a run stopped. This
breaks the spirit of AGENTS.md rule 3, and it is why the previous audit had to trace four files
to find one stop rule. It is also why `worker_failure` classifies these stops as "provider".

**B2. The verifier carries hidden decision state.** `CodingVerifier` holds `checked`,
`repetitions`, `blocked` and `last_failure`. The runtime consumes them through `repeat=True` and
the wrapper consumes them through `blocked`. A verifier should be a pure measurement. Caching and
blocking are policy.

**B3. Prompts are embedded in composition code.**

- The worker's native protocol text is assembled inside `build_coding`, with 7 conditional
  fragments.
- The run objective suffix is assembled inside `CodingCoordinator.drive`, with 4 profile branches.
- The supervisor `SYSTEM` prompt is a literal in the adapter.
- The review prompt is a literal in `GeminiReviewModel`.
- Per-project prompts for Brook are hard-coded in `campaign.py`.

Prompt changes are therefore code changes in composition functions of up to 395 lines, and
cannot be versioned, diffed or tested as prompts.

**B4. Credentials and adapters are created deep inside business logic.**

- `CodingCoordinator.drive` calls `load_gemini_key(Path(".loopforge/secrets.env"))` and builds
  `GeminiReviewModel` mid-run.
- `campaign_repair_escalation` does the same.
- The key path is repeated in 8 source files and 3 scripts.

**B5. The supervisor contract has two sources of truth.** Domain rules (which arguments each
action takes, dataclass bounds) and Gemini's wire schema and prompt are maintained by hand. See F7
in the previous audit.

### C. Integration and composition

**C1. There is no single composition root. The goal stack is assembled in at least five places,
and they differ.**

| Composition site | Supervisor wiring | Evidence store | Recording | Token counting |
|---|---|---|---|---|
| `goal_launch.recorded_drive` (the day-to-day command) | `RecordedSupervisor(GeminiSupervisor(...))` | Attached by hand | Yes | Yes |
| `goal_runner._drive` (**console server path**) | Bare `GeminiSupervisor(...)` | Via `isinstance` | No | No |
| `repository_supervisor.main run` | Bare | Via `isinstance` | No | No |
| `supervisor.main run` (prepared goals) | Bare | Via `isinstance` | No | No |
| `autonomy_qualification.qualify` | Its own | Its own | Yes | No |

The path validated in trials (`launch`) is not the path the packaged console uses. Bug C in the
diagnosis was one instance of this class.

**C2. Wiring happens by mutating objects after they are built.**

- **Harness settings:** `harness.nonblocking_goals`, `operator_stop` and
  `provider_request_directory` are set in three places.
- **Coding coordinator fields:** `harness.coding.parent_budget`, `execution_deadline_epoch`,
  `driver`, `live_observer` and `atomic_finish`.
- **Tools and runtime:** `build_coding` constructs `Runtime`, then sets 7 tool attributes and
  re-wraps `runtime.model`, `runtime.context` and `runtime.tools`.

An object is never complete after construction, and the correct order of mutations is implicit.

**C3. A shared mutable dictionary is used as a message channel.** The goal layer writes
`harness.coding.assignment_guidance[task]` from three places. The coding coordinator reads it in
three places, including to decide whether a retry is "the same decision problem".

**C4. Concrete-type checks and decorator unwrapping cross boundaries.**

- `isinstance(supervisor, GeminiSupervisor)` decides evidence wiring.
- `isinstance(self.model, OllamaModel)` mutates a provider's timeout from a wrapper.
- `campaign_execution` unwraps `RepairGuidanceTools` and `RepairDecisionTools` decorators to reach
  `ConfinedCodingTools`.

Each of these silently stops working when a wrapper is added.

**C5. One assignment passes through four state machines and five SQLite files.**

- **State machines:** repository goal (`GoalState`) → campaign (`CampaignState`) → coding task
  (`CodingState`) → run (`RunState`), plus the paid budget, repair journal and runner record.
- **SQLite files per goal:** `goals.sqlite`, `paid-budget.sqlite`, `runner.sqlite`,
  `worker/campaigns.sqlite`, `worker/runs.sqlite`.
- No transaction spans them, so consistency rests on bespoke reconciliation
  (`goal_worker_operations`, `recover_interrupted`, `reconcile`).
- `CampaignGoalWorker.observe` queries another component's tables directly with SQL
  (`coding_events` with `json_extract`).
- The campaign state machine (stages, lanes, rounds) serves the older review-campaign product.
  For goals it is only an obstacle: "attention" has to be resumed before each dispatch.

**C6. Two products are fused.** The Brook/HeliCops review-campaign product (`campaign.py`,
lanes, rounds, issues, its own FastAPI app and CLI) and the repository-goal product share the
coding coordinator and the campaign store. Goal work inherits campaign concepts it does not use
(stage 1 "hardening only", the launch-branch rules for `helicops-cloud`).

### D. Configuration

**D1. Seven configuration sources, with no single owner.**

1. The intake JSON (`RepositoryGoalConfig` → `CampaignConfig` + `CodingTaskConfig`)
2. `GoalPolicy`, derived from the intake and frozen
3. `OllamaProfile`, sampling and context in code
4. The `inference_profile` string, which switches behaviour
5. 95 module-level numeric constants
6. Inline literals in control code: no-progress `min(8, max_iterations)`, no-new-evidence `8`,
   blocked-after `3`, circuit `3`, `max_cost_usd=1`, context budget `24_000`, evidence cap
   `24_000`, history caps `48_000`/`96_000`, detail budget `2000`
7. CWD-relative paths and `ServerSettings` defaults

**D2. Profiles are behaviour flags spread across files.**

- `inference_profile` has 8 values, compared by string in 24 places across 5 files.
- `"qwen-native-v1"` alone turns on about 10 separate behaviours:
  - multi-read, edit echo, measured checks and construction credit
  - the import formatter, conversation bookkeeping and the template version
  - the objective text, review app mode and the history limit
- There is no record listing what a profile does.
- "Profile" also means two other things: `OllamaProfile` (sampling) and `LoopProfile` (operator
  session presets in `profile.py`).

**D3. The thresholds that decide whether a run lives or dies are not configuration.** Iterations,
tokens and time are intake-configurable. No-progress, no-new-evidence, the circuit threshold and
the check-blocked count are literals. The supervisor is never told about them, and a trial cannot
freeze them.

**D4. Benchmark projects are hard-coded into the generic product.**

- `CampaignConfig.project` is `Literal["brook", "helicops-cloud", "snake", "reconciliation"]`.
  The product cannot target any other repository without a code change.
- `campaign.py` contains hand-written prompts keyed by `(project, lane, scope, round)`.
- `cli.py` has a CivicML-only `civicml-loop` command.

**D5. Budgets are expressed in three units, in three places:**

- The worker uses USD, tokens, seconds and iterations per run, plus tokens and seconds per task.
- The goal uses micro-USD, decisions and a deadline.
- The parent paid budget is a separate SQLite file.

The interactions are implicit. For example, `goal_runner` clamps worker seconds by the goal
deadline through a mutated `execution_deadline_epoch`.

### E. Modularity and size

**E1. God modules and functions.**

- Modules over 1,000 lines: `campaign_coding` (1,870), `campaign` (1,516), `cli` (1,447),
  `campaign_review` (1,422), `eval` (1,064), `sessions` (1,039).
- 38 functions exceed 100 lines. The largest are:

  | Function | Lines |
  |---|---|
  | `server.create_app` | 517 |
  | `telemetry.project_event` | 403 |
  | `build_coding` | 395 |
  | `CodingCoordinator.drive` | 364 |
  | `cli.main` | 331 |
  | `campaign.create_app` | 319 |
  | `campaign_state_context.build_context` | 221 |
  | `GeminiSupervisor.prepare` | 211 |
  | `OllamaModel.__init__` | 204 |

**E2. Legacy paths are still wired through the hot path.**

- Recent trials use only `qwen-native-v1`, with `qwen-bounded-v3` as the known-good control.
- `baseline-v1`, `qwen-coding-v1`, `qwen-bounded-v2`, `qwen-app-v1`, `qwen-state-v1` and
  `qwen-session-v1` are all still branched on in `build_coding` and `drive`.
- Repair escalation and repair decisions (about 1,030 lines, `qwen-state-v1` only) are among
  those branches.
- Every change to composition has to reason about all 8 profiles.

**E3. Duplication.**

- 14 inline sha256-hex validations.
- 7 path-safety helpers with overlapping rules: `relative_path`, `source_allowed`, `within`,
  `writable`, and three copies of `_path_allowed`.
- Two FastAPI apps and four console scripts.
- Gemini HTTP, envelope and usage handling duplicated between `gemini_supervisor` and
  `gemini_review`.

### F. Contracts and errors

**F1. `ValueError` is used as general control flow.** There are 37 `except ValueError` sites. In
`GoalCoordinator`, a `ValueError` can mean any of these:

- a race on the optimistic-concurrency append, which is retried
- a reducer invariant violation, which is a bug
- a worker transition refused by state, which is an expected rejection
- an invalid assignment, which is model input

Each meaning should be handled differently. Today `_run_worker` turns all of them into a terminal
`blocked` (previous audit F5). The runtime has typed errors (`InvalidTransitionError`,
`ToolContractError`). The goal layer has none.

**F2. The goal journal has no schema version.** Run events carry `schema_version: 1` through
`JsonEventCodec`. Goal events (`adapters/goal_journal.py`) are a bare discriminated union, and
compatibility is handled ad hoc ("old streams decode as False").

### G. Engineering process

- **Unchecked branch:** 48 commits and +41K lines were never pushed and never ran in CI. One
  commit, `bf1b287`, carries 190 files.
- **CI is currently red:** the format check fails, and the lockfile drifted.
- **Tests are organised by incident rather than by behaviour:** `test_audit_*_regressions`,
  `test_campaign_audit_remediation`, `test_autonomous_audit_controls`. The 6,165 lines of
  `test_campaign*` mirror history, not design.
- **One goal-level integration test,** and it is a happy path.
- **Trial runners as product code:** the 18 `.loopforge/tasks/*/run.py` scripts were the real
  composition until `launch` was extracted from them.
- **Blanket suppressions:** `noqa` reasons are copied instead of the code being refactored.

### What is good, so it is kept

- The domain events, reducers and replay. `RunState` is a model of how to do this.
- Ports for model, tools, verifier, sandbox, store, clock and supervisor.
- Code-owned tool authority and the trust-class vocabulary (AGENTS.md rules 4 and 16).
- The sandbox capability contracts.
- The strict typing discipline, and no `Any` in the core.
- AGENTS.md itself. It states the right rules; the product layer is simply outside their
  enforcement.

## 3. Target shape

```
loopforge/
  domain/           + gates.py      one GatePolicy: every stop/limit rule as a pure function of
                                    journaled facts (runtime, worker, goal), with its threshold
                    + coding.py     coding/review lifecycle (exists), path policy (one helper)
  config/           NEW             Settings (absolute paths, credentials provider), GoalIntake,
                                    WorkerProfile (explicit behaviour record per profile),
                                    GateLimits (frozen into the intake/policy)
  ports/            + stores (goal/coding/runner), credentials, prompts
  application/      GoalCoordinator (exists), CodingService (from CodingCoordinator),
                    InvestigationService, WorkerSession; typed errors
  workloads/coding/ ConfinedCodingTools, CodingVerifier (pure measurement), probe scoring
  workloads/review/ ReviewTools, review acceptance
  adapters/         gemini (shared HTTP/envelope core; supervisor + reviewer), ollama, mlx
                    (server + renderer packaged), sqlite stores, recording decorators
  prompts/          versioned prompt templates, rendered from WorkerProfile + domain contract
  bootstrap.py      THE composition root: build_goal_runner(settings, intake) -> GoalRunner
  entrypoints/      thin: cli, server, console, launch — parse, call bootstrap, print
  campaigns/        the Brook review-campaign product, isolated (or retired; see decisions)
```

Enforcement, so the standards hold:

- **import-linter:**
  - `entrypoints` may import only `bootstrap`, `config`, `application` and `ports` (plus FastAPI
    and argparse glue).
  - `campaigns` and the goal product are independent.
  - `workloads` must not import `adapters`.
- **Architecture tests:**
  - no `sqlite3` or SQL outside `adapters`
  - no `Path(".loopforge` literals outside `config`
  - no `inference_profile ==` comparisons
  - every stop reason code is defined in `domain/gates.py`
  - every drive path builds its stack through `bootstrap`
- **Ruff:** keep the complexity rules on and track existing `noqa` suppressions in a ratchet file
  that may only shrink.
- **CI:** push the branch and require green before any live trial.

## 4. Migration plan

Every step is behaviour-preserving and lands green, with a test that pins the old behaviour
first. Bug fixes from the previous audit come after the step that gives them a single home.

| Step | Change | Unblocks |
|---|---|---|
| 0 | Hygiene: format the 4 files, commit the lock, push the branch, get CI green, and commit in reviewable units from now on | Trustworthy gates |
| 1 | **One composition root** (`bootstrap.build_goal_runner`), used by `launch`, the console server, both `main run` commands and qualification. The recording and evidence-store wiring become decorators that forward a port method. Test: every entry path produces an identical stack description. | Previous audit F16; console and launch divergence |
| 2 | **Configuration:** `Settings` (absolute paths, credentials provider, MLX lab location), a `WorkerProfile` record replacing the 24 string comparisons, and `GateLimits` with today's values frozen into new intakes. Test: each profile's record equals today's derived behaviour. | D1–D3, A3 (paths) |
| 3 | **One gate policy:** move the wrapper and verifier stop rules into `domain/gates.py` as functions of journaled facts. The facts are recorded as events (no-new-evidence streak, check-blocked, stalled retry). The runtime stops through `RunStopped` with a gate reason code, not a fake model failure. Add the gate inventory test. | Previous audit F1, F2, F3, F11, F17 become one-file changes; S1 |
| 4 | **Move the coding and review workloads** out of `entrypoints` into `workloads/`, and the stores into `adapters` behind ports. Remove cross-component SQL. Break both import cycles. | A1, A2, C5 (partially) |
| 5 | **Typed goal errors** (race, invariant, rejection, model input), a goal-journal schema version, and the supervisor wire contract generated from the domain map | F1, F2, B5, previous audit F5 and F7 |
| 6 | **Retire or isolate legacy:** unused profiles, repair escalation, and the Brook campaign product (per decisions) | E2, C6, D4 |
| 7 | **Then the bug plan** of `full-audit-2026-10-24.md`, Phase A onward, on the new structure | — |

Steps 1–3 deliver most of the value and keep each change small. Steps 4–6 are mechanical once
those are in.

## 5. Decisions for the user

1. **Legacy profiles.** May `baseline-v1`, `qwen-coding-v1`, `qwen-bounded-v2`, `qwen-app-v1`,
   `qwen-state-v1`, `qwen-session-v1` and repair escalation be removed? Their recorded trials stay
   readable. Recommendation: keep `qwen-native-v1` and `qwen-bounded-v3` (the known-good
   control), and remove the rest.
2. **The Brook/HeliCops review-campaign product.** Isolate it in its own package, or retire it?
   Recommendation: isolate it now and retire it later if unused. Either way, the goal product
   stops depending on campaign stages and lanes.
3. **Target repositories.** Replace the `project` Literal with free-form intake data, so goals can
   target any repository. Recommended.
4. **CivicML command and Postgres server path.** Keep or drop? Neither is on the goal path, so
   there is no urgency.
5. **Persistence.** Keep five SQLite files per goal, or consolidate into one database with one
   transaction per step? Recommendation: consolidate during step 4. It removes most of the
   reconciliation code.
