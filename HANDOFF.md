# LoopForge agent handoff

## Restoration note (2026-08-23)

This repository was restored from a damaged git-bundle import. The original 9-commit
history (`528ff05` … `a379e6e`) could not be pushed because the bundle was missing
objects. The working tree at tip commit `a379e6e` was recovered almost in full and
committed as a new root commit; the recovered original commits survive locally on the
`recovered-tip` branch of the maintainer's machine (incomplete object graph, not
pushable).

Known gaps versus the original PACS-005 tree:

- The original `tests/unit/`, `tests/security/`, and `tests/resilience/` files were
  lost with the damaged bundle chunks. All three suites were rebuilt during the
  2026-08-23 hygiene pass (696 tests passing, 95.75% branch coverage, all lint/type
  gates green — see BUILD_STATUS.md). The rebuilt tests pin current behavior and
  recovered semantics; they are not the byte-identical originals.
- `docs/process/manual-pacs.md`, `docs/process/remaining-pacs-plan.md`, and
  `docs/process/cycles/PACS-005-security-sandbox-contract.md` were regenerated from
  the originating chat session; content is substantively correct but not
  byte-identical to the originals.
- `docs/process/fault-injection-laboratory.md` content was recovered; its original
  directory placement is uncertain (currently under `docs/process/`).

## Read this first

LoopForge is a reference implementation and experimentation platform for bounded autonomous software-engineering agents.

The central thesis is:

> A probabilistic model may optimize within explicitly granted authority, but the deterministic runtime owns authority, state, verification, side effects, budgets, recovery, and termination.

The experimental extension is:

> Execution history may improve adaptive execution policies, but adaptive systems may never expand their own authority.

## Current checkpoint

Completed: PACS-001 through PACS-014.

PACS-014 (operator command center, 2026-09-02) gave the operator a durable
authority channel and a live console without ceding any runtime authority.
Scope was confirmed up front as operator control + console only — the
evaluator and evidence-grounded Reflexion portions of the originally planned
PACS-014 are deferred (operator decision 1, dated). The schema-v1 event
catalog grew 22→24 (operator-signed-off): `ApprovalRejected` and
`OperatorInstruction` through all five touchpoints, reusing
`WAITING_FOR_APPROVAL` for the pause (no new PAUSED status). Approval-gated
tools quiesce a run on durable `ApprovalRequested`; runtime
`grant_approval`/`reject_approval`/`add_operator_instruction` move it, with
permissions re-authorized on the approved path (a grant never expands
authority, and is spent by exactly one completed execution — a live-found
stream-poisoning defect, fixed and pinned). Production gating is operator
wiring: `ApprovalGateTools` + profile `[approval] required_for` (adapters
honestly classify `approval=NONE`; unknown gated names fail closed at bundle
build). A runs-index projection (`StateStorePort.list_runs`, store schema v2
with migration precedent) feeds session enumeration; a `PostgresEventStore`
mirrors SQLite semantics exactly (append-only triggers, compare-and-append,
12-test live conformance suite, docker-compose Postgres). The FastAPI server
(D2/ADR-0010 dependency relaxation) is projection + command issuer only:
one driver thread per run (D5), fan-out strictly after durable append (D6),
restart rediscovery from the runs index + session registry (D9), no auth on
127.0.0.1 (D10). The `ui/` Vite+React+TS console (sessions list, WS-driven
session detail with reconcile-on-reconnect, approval banner, diff viewer,
selective rollback, objective amendment) builds to static assets mounted by
the backend. The §10 live round-trip passed end-to-end: local Ollama
session paused on the approval-gated write, approved in the GUI, executed
in the container sandbox, verifier-granted success, patch in the diff
viewer, selective rollback of one file, and server restart resuming the
session from Postgres. Adversarial review ran continuously; every finding
fixed and pinned — see `docs/process/cycles/PACS-014-operator-command-center.md`.
Checkpoint committed as `29d6e85`.

PACS-013 (orchestrator/worker and worktree isolation, 2026-08-30) added bounded
multi-agent execution as an opt-in, benchmarkable path without ceding any
runtime authority. The schema-v1 event catalog grew 19→22 (operator-signed-off):
`WorkerSpawned`/`WorkerStopped`/`WorkerMerged` through all four touchpoints,
with a replayable `RunState.workers` roster projection that transitions the
orchestrated run to `VERIFYING` only when every worker is stopped and every
succeeded worker has a merge outcome. A durable, workload-agnostic
`Orchestrator` owns the global plan on its own authoritative stream, spawns
bounded workers with durable ownership records (worker id, worker run id,
workspace id, objective, static budget share), drives worker runtimes
round-robin one cycle at a time through the runtime's new `step()` seam
(drive-loop locals became per-run `_DriveState`; blocking `run()`/`resume()`
semantics pinned unchanged), records terminal outcomes, reconciles succeeded
workers in spawn order via real Git merges (`--no-ff`; conflict → abort →
`WorkerMerged(CONFLICT)` → explicit `WORKER_MERGE_CONFLICT` `FAILURE` stop),
verifies the merged workspace, and records merged evidence through the
existing artifact path. Workers execute in isolated linked Git worktrees
(`git worktree add -b worker/<id>`) with the metadata-fingerprint defense
extended to the `.git` pointer layout; budgets are static shares of the
global `BudgetLimit` (`partition_budget`, shares validated to never sum above
the global limit) enforced by each worker's own `ControlPolicy` — shares,
never new authority (rule 12). The two-module calculator fixture decomposes
repair across two workers with disjoint patch constraints; the
`orchestrated-repair-demo` CLI wires the path (scripted default,
`--container` variant) while the single-runtime `repair-demo` stays
byte-identical — multi-agent is never a default. Live container E2E: two
workers succeed, merge in spawn order, and the integration verifier grants
success on the full merged suite. A mid-cycle, operator-visible scope
addition (DeepSeek adapter + `civicml-loop` dogfooding, commits `d12d71a`..
`3a26f27`) is recorded as a deviation in the cycle record. Adversarial
hardening ran as a continuous two-agent review (Kimi + Codex) interleaved with
construction — every finding fixed and pinned, most severely the non-durable
first orchestrator (rewritten event-sourced), a transient-failure fallback
regression from the `step()` seam refactor, and a verifier blind spot where
committed merges made integration `require_change` unsatisfiable. See
`docs/process/cycles/PACS-013-orchestrator-worker-and-worktree-isolation.md`.
Checkpoint committed as `5bb4f05`.

PACS-012 (model capability registry and routing, 2026-08-28) replaced hard-coded
model selection with a code-owned registry and deterministic, reason-coded routing
— without ceding any authority. `ModelCapabilities` moved home to
`domain/routing.py` (mirroring `SandboxCapabilities`) beside `ModelTier`
strength/cost classes, fail-closed `ModelRequirements`, the closed `RouteReason`
vocabulary (7 codes), and an authority-free `RoutingPolicyConfig`; `ModelPort`
now declares `capabilities` structurally and `ScriptedModel` advertises honest
defaults. `ModelRegistry` fails at construction on duplicate provider/model
registration and fails closed on capability mismatch; it is where honest
per-model wiring-time metadata lands (the CLI now registers the Ollama model's
real context window via `--ollama-context-window`). `TieredRoutingPolicy`
selects cheapest-at-target-tier (climbing only when the tier is empty),
escalates vertically on stalls, de-escalates on budget pressure using a
read-only remaining-budget fraction (enforcement stays with `ControlPolicy`,
rule 12), falls back horizontally across providers on transient failure, and
retains honestly when no compatible move exists. The runtime's optional
`router` seam selects the model per turn (mid-run swaps restart the adapter
conversation from durable context — the PACS-011 resume safety argument); a
model-less decision stops `FAILURE` with `ROUTE_NO_COMPATIBLE_MODEL` durable in
the existing `RunStopped` event (schema v1, 19 events unchanged). Telemetry
gained one closed-vocabulary span (`loopforge.model.route`); router-less runs
are byte-identical to PACS-011. The policy is proven entirely with fake
adapters; the live Ollama+Docker repair E2E passes through the routed runtime.
A post-cycle adversarial hardening pass (three-agent review, 2026-08-28) triaged
~15 findings; every actionable defect was fixed and pinned — most severely a
same-provider "fallback" mislabeled as cross-provider, and a `default_tier`
above all registered tiers wedging runs non-terminal (now clamped: the default
tier is a preference, requirements the hard gate) — plus mislabeled
first-selection reason codes, no-op budget pressure suppressing escalation, a
latent routed-model client leak in bundle `close()`, Ollama capability/request
identity divergence, and contract-validation normalization gaps. See
`docs/process/cycles/PACS-012-model-capability-registry-and-routing.md`.
Checkpoint committed as `48f2aac` (includes the operator-initiated post-cycle
hardening pass).

PACS-011 (first live model adapter, 2026-08-27) integrated a real provider behind
`ModelPort` without ceding runtime authority: an `OllamaModel` adapter (Ollama native
`/api/chat` over `httpx`, the first HTTP dependency) that consumes only budgeted
`ModelContext` + the versioned prompt contract, validates responses by strict schema
(exactly one tool call, `StrictStr` arguments), normalizes provider failures into a
port-level `ModelFailureClass`/`ModelTurnError` taxonomy (no provider semantics in domain
code), debits real token counts through the PACS-008 accounting surface, exposes
`ModelCapabilities` metadata, and isolates the optional bearer credential to request
headers (environment-sourced, never in context/events/`repr`). The runtime gained the
model-turn failure seam: permanent failures and exhausted transient streaks stop
`FAILURE` with durable reason codes (no new event types — schema v1, 19 events), while
transient failures retry with bounded backoff outside the iteration budget. The key
discovery: flattened text observations make live models re-read instead of act — the
adapter maintains per-run conversational state and answers pending tool calls with
structured `tool` messages built from observation-trust context deltas (proven by A/B
probe). Live-model tests are separated into `tests/live/` behind Ollama+Docker skipif
probes; deterministic CI runs with zero credentials. Live evidence: `repair-demo
--container python:3.12-alpine --model ollama` with `devstral-small-2:latest` →
`status=succeeded iterations=2`, verifier-granted, exact patch recorded. A post-cycle
adversarial hardening pass (three-agent review, 2026-08-28) triaged ~20 findings; every
actionable defect was fixed and pinned — most severely a REFLECTING-resume wedge that
durably poisoned the event stream (drive loop now re-plans from REFLECTING), plus
exception-classification gaps (`httpx.DecodingError`, HTTP 408), NaN/inf cost rates,
dropped trust labels at the conversational boundary, rejected no-arg tool calls,
constructor validation, unbounded durable stop text, failure-class coercion, and CLI
error-path leaks; seven findings documented as designed. See
`docs/process/cycles/PACS-011-first-live-model-adapter.md`. Checkpoint committed as
`975cf12`.

PACS-010 (software-repair workload and deterministic verifier stack, 2026-08-27) made software
repair the reference workload while keeping the runtime workload-agnostic: a
`WorkspaceManagerPort` with a Git-backed adapter (status/diff/checkout, symlink-safe diff
rendering), safe file read/search/edit tools, predefined sandbox commands, a deterministic
`RepairVerifier` (command outcomes + patch constraints + contract-checked acceptance hooks;
model output never overrides it), workspace snapshot/diff evidence as the durable, replayable
19th event `ArtifactRecorded` (schema v1; telemetry sees metadata only), hermetic offline
fixture repositories, and a `repair-demo [--container IMAGE]` CLI. Untrusted repair tools
declare code-owned `SandboxRequirements(process_filesystem_isolated=True,
network_isolated=True)` and fail closed anywhere `ContainerSandbox` is unavailable; fixture
content enters context only as `TrustClass.UNTRUSTED_CONTENT`. Live container E2E repairs the
adder-regression fixture in 2 iterations with the exact patch recorded. An operator-initiated
post-cycle adversarial hardening pass fixed and pinned ~10 defect clusters — most severely,
hostile repository content driving host-side Git execution (a planted `.git/config` textconv
via the rw bind mount; now blocked by a materialize-time metadata fingerprint verified before
every host Git invocation) — plus verifier/evidence blind spots (ignored files invisible to
`status()`), fail-open verifier edges, a permanent VERIFYING wedge on artifact-collector
failure, crash/resume evidence duplication, NaN scores encoding into undecodable streams, and
docker-argv flag injection via leading-dash image names. See
`docs/process/cycles/PACS-010-software-repair-workload.md`. Checkpoint commit: `c65e051`
(includes the operator-initiated post-cycle hardening pass).

PACS-009 (hardened container sandbox, 2026-08-27) added `ContainerSandbox`: a Docker-CLI-driven
`SandboxPort` adapter that executes untrusted repository/build workloads in hardened containers
(workspace-only bind mount, read-only rootfs, deny-all network policy, dropped capabilities,
`no-new-privileges`, memory/pids/CPU/nofile/tmpfs limits, explicit environment filtering, bounded
output, PID-namespace-destroying timeout kills, and `destroy()` that leaves no running child
workload). It composes `ConstrainedLocalSandbox` for the file API (defenses unchanged), honestly
advertises `process_filesystem_isolated=True`/`network_isolated=True` while keeping
`kernel_isolated=False`, and proves fail-closed capability negotiation in both directions. Live
isolation tests are capability-gated so deterministic CI passes daemon-free. See
`docs/process/cycles/PACS-009-hardened-container-sandbox.md` and ADR-0009. Checkpoint commit: `e3963e5` (includes the operator-initiated post-cycle hardening pass).

PACS-008 (observability foundation, 2026-08-27) added an OpenTelemetry-compatible telemetry
vocabulary and data model, a `TelemetryPort` with a fail-safe emission boundary, deterministic
causally-correlated traces (run/cycle/operation spans), structured logs and metrics projected
from the authoritative event stream, code-owned pre-emission redaction (`SensitiveText`,
SENSITIVE/SECRET → `[redacted]`), correlation identifiers (run/worker/cycle/action/tool/
attempt/verification), token/cost/cache accounting, OTLP/JSON converters proven offline, and a
`TelemetrySandbox` span decorator. Telemetry is a non-authoritative projection: it is emitted
only after durable appends, never feeds back into runtime decisions, and adapter failures
cannot corrupt run state. See
`docs/process/cycles/PACS-008-observability-foundation.md`. Checkpoint commit: `a4e1bea`.

PACS-007 (context lifecycle and prompt contracts, 2026-08-27) added deterministic context
selection with hard token budgeting, explicit per-item accounting, preservation contracts,
structured compaction/pruning, role-specific assembly (`ModelRole`), and versioned prompt
templates whose id/version are persisted on every `ContextAssembled` event. See
`docs/process/cycles/PACS-007-context-lifecycle-and-prompt-contracts.md`. Checkpoint commit: `1dcad71`.

PACS-006 (context authority model, 2026-08-25) added typed/provenance-aware `ContextItem` /
`ModelContext` artifacts, the guarded trust-elevation path, `ContextBuilderPort`, and the durable
`ContextAssembled` event; the model boundary now consumes `ModelContext` instead of raw
`RunState`. See `docs/process/cycles/PACS-006-context-authority-model.md`. Checkpoint commit: `10ee331`.

Recent commits, newest first:
- `624658d` security: enforce sandbox capability requirements
- `e4e5216` feat: add security and sandbox contract
- `c7ddcc5` test: add fault-injection laboratory
- `4a692cc` feat: add reliability control plane
- `c6e965d` feat: add durable sqlite event store
- `9ead90b` feat: close v0.1 kernel contract
- `1929cb5` docs: add master product map and manual PACS process
- `528ff05` feat: establish deterministic LoopForge runtime kernel

Current checked evidence (PACS-013, 2026-08-30, Ollama 0.32.15 with
`devstral-small-2:latest`, Docker Desktop live):
- 1449 tests passing, 18 platform-gated skips (the pre-existing macOS `RLIMIT_AS` +
  non-UTF-8 gates plus 3 new trusted-local orchestrated E2E/CLI gates on the same
  platform restriction; the live Ollama+Docker repair E2E and the live container
  orchestrated two-worker E2E both executed and passed; zero credential-gated
  skips — deterministic CI runs with zero provider credentials, 1448 passing with
  `--ignore=tests/live`)
- 94.34% branch-aware coverage (new orchestration modules at 92–98%)
- ruff format/check, pyright strict, and import-linter all executed and green
- live CLI evidence: `orchestrated-repair-demo --container python:3.12-alpine` →
  `status=succeeded stop_reason=success_verified`, both workers
  `outcome=succeeded merge=merged`, integration verifier `command:run_tests:
  passed (exit_code=0)`, merged evidence artifact recorded, orchestrator stream
  replaying to the exact terminal state
- cycle discoveries fixed and pinned: integration `require_change` was
  incompatible with committed merges (worker patches land as merge commits, so
  status-based change detection sees a clean tree) — integration acceptance now
  gates on the full merged suite while per-worker acceptance enforces
  `require_change` pre-merge; container runs must wire the in-container
  interpreter (`/usr/local/bin/python`)

Historical evidence (PACS-012 post-hardening, 2026-08-28, Ollama 0.32.15 with
`devstral-small-2:latest`, Docker Desktop 29.2.1 live):
- 1370 tests passing, 15 platform-gated skips (same macOS `RLIMIT_AS` + non-UTF-8 gates as
  PACS-010/011; the live Ollama+Docker repair E2E executed and passed through the routed
  runtime; zero credential-gated skips — deterministic CI runs with zero provider
  credentials, 1369 passing with `--ignore=tests/live`)
- 96.71% branch-aware coverage (all new routing modules at 97–100%)
- ruff format/check, pyright strict, and import-linter all executed and green
- live CLI evidence: `repair-demo --container python:3.12-alpine --model ollama` →
  `status=succeeded iterations=2` with the live model driving, verifier summary
  `command:run_tests: passed (exit_code=0); patch_constraints: passed (files changed:
  adder.py)`, and the exact `adder.py` patch recorded as durable evidence
- cycle discoveries fixed and pinned: conversational tool-result messaging (the
  flattened-observation re-read loop), code-owned objective naming the workspace layout,
  and a too-strict `/api/tags` probe that silently skipped the live suite

Historical evidence (PACS-010 + post-cycle hardening pass, 2026-08-27, Docker Desktop
29.2.1 live):
- 1237 tests passing, 15 platform-gated skips (9 pre-existing `RLIMIT_AS` + 4 trusted-local
  repair E2E + 1 repair-demo CLI smoke + 1 non-UTF-8-filesystem gate; **zero** docker-gated
  skips — all 13 live container isolation tests and the live container repair E2E executed
  against the real runtime)
- 96.44% branch-aware coverage
- ruff format/check, pyright strict, and import-linter all executed and green (layers contract
  now includes `workloads`)
- live CLI evidence: `repair-demo --container python:3.12-alpine` → `status=succeeded
  iterations=2` with the exact `adder.py` patch and verifier evidence recorded
- cycle discoveries fixed and pinned: `python -B` in all fixture commands (same-second edits
  defeated by stale `__pycache__`), and fully-qualified-reference fallback in both live docker
  image probes (containerd store short-name `inspect` resolution failure)
- post-cycle hardening fixed and pinned (see `tests/regression/test_hardening_regressions.py`
  PACS-010 section and the cycle record): `.git/config` tamper → host-Git refusal via
  materialize-time metadata fingerprint + hermetic Git environment; ignored files reported as
  worktree deviations and reclaimed by `clean -fdqx`; literal pathspec checkout from the base
  revision; non-UTF-8/control-char filename handling; non-string tool-argument failures;
  fail-closed verifier hooks and sanitized detail strings; vacuous acceptance-criteria
  rejection; `ARTIFACT_COLLECTION_FAILED` terminal stop (+ double-stop guard); evidence
  dedup by content fingerprint; artifact label/content and finite-score domain validation;
  leading-dash container image rejection; clean CLI error for blank `--container`

Historical evidence (PACS-009 + post-cycle hardening pass, 2026-08-27):
- 1091 tests passing, 9 platform-gated skips (13 live container tests executed against a real
  Docker runtime; daemon-free run skips them with reason codes and stays green)
- 97.86% branch-aware coverage (`adapters/container_sandbox.py` at 100%)
- ruff format/check, pyright strict, and import-linter all executed and green
- deterministic demo with correlated telemetry narrative, compile, architecture DAG checks passing
- post-cycle hardening fixed and pinned: raw `OSError` from a missing Docker binary, the
  kill-before-start race (now a bounded retry loop), comma-containing workspace roots corrupting
  `--mount` parsing, and NaN/Infinity bypass of timeout/limit validation in both sandbox adapters

Historical evidence (PACS-008, 2026-08-27):
- 1013 tests passing, 9 platform-gated skips
- 97.73% branch-aware coverage
- ruff format/check, pyright strict, and import-linter all executed and green
- deterministic demo with correlated telemetry narrative, compile, architecture DAG checks passing

Historical evidence (PACS-007 + post-cycle hardening pass, 2026-08-27):
- 897 tests passing, 9 platform-gated skips
- 97% branch-aware coverage
- ruff format/check, pyright strict, and import-linter all executed and green
- deterministic demo, compile, architecture DAG checks passing

Historical evidence (PACS-006 + post-cycle hardening pass, 2026-08-25):
- 805 tests passing, 9 platform-gated skips
- 96.31% branch-aware coverage
- ruff format/check, pyright strict, and import-linter all executed and green
- deterministic demo, compile, architecture DAG checks passing

Historical evidence (restoration + hygiene pass, 2026-08-23):
- 696 tests passing, 9 platform-gated skips
- 95.75% branch-aware coverage
- ruff format/check, pyright strict, and import-linter all executed and green
- deterministic demo, compile, architecture DAG checks passing

Historical PACS-005 evidence: 125 tests passing, 93% coverage, security suite 22/22,
with Ruff/Pyright/Import Linter unavailable in that environment.

## Mandatory source documents

Read before modifying architecture:
- `docs/product/master-build-map.md`
- `docs/process/manual-pacs.md`
- `docs/process/remaining-pacs-plan.md`
- `docs/architecture/overview.md`
- `docs/security/threat-model.md`
- `AGENTS.md`
- `BUILD_STATUS.md`

Read the preceding cycle record before starting the next one:
- `docs/process/cycles/PACS-014-operator-command-center.md`

## Next authorized work

PACS-014b (operator usability) closed 2026-09-02 — see
`docs/process/cycles/PACS-014b-operator-usability.md`: no_progress_limit is
now a profile/inline knob (default 3), the inline form supports container
mode (the macOS-viable path), terminal runs offer a manual report-seeded
follow-up, and a live container run drove the blackjack demo to
STOP_SUCCESS_VERIFIED.

PACS-015 through PACS-017 remain PLANNED, not active; the evaluator +
evidence-grounded Reflexion remainder of the original PACS-014 scope is also
available as an explicitly named follow-on. PACS-014b follow-ons worth
considering for PACS-015 planning: a dedicated PG test database (the suite's
schema drop annihilates live sessions on the default DSN), a repository-claim
force-release/expiry path for zombie runs, and a progress model that
distinguishes exploration from no-progress.

PACS-014 (operator command center) closed 2026-09-02 at 1592 tests passing
(18 platform-gated skips), 95% branch coverage, all lint/type/import gates
green, UI typecheck/build green, and the §10 live operator round-trip
complete against Postgres in Docker and a local Ollama model. The schema-v1
catalog extension 22→24 was explicitly signed off as operator decision 2 of
the cycle; the serving-plane dependency relaxation is ADR-0010 (operator
decision 3). The operator may manually initiate the next cycle.

### Operator-authorized addition (2026-09-01): profile-driven `loop` command

Mid-cycle, operator-visible scope addition (same class as the PACS-013
DeepSeek/`civicml-loop` deviation): a generic `loopforge loop <profile.toml>`
command that runs the bounded adopted-checkout repair loop against ANY local
repository — including LoopForge itself — driven by operator-owned TOML
profiles instead of the hardcoded `civicml-loop` wiring.

- `src/loopforge/entrypoints/profile.py` (new): `load_profile()` /
  `LoopProfile` / `ProfileError`. Validates every field at load time and
  fails closed: absolute existing git-worktree repository, 1..N uniquely
  named checks with `TEST|LINT|TYPECHECK|BUILD` kinds, absolute (or
  `{python}`-token, local mode only) argv executables, `required ⊆ checks`,
  relative no-`..` patch prefixes, env-key allowlist pattern, container image
  reference rules mirroring `ContainerSandboxConfig`, provider/tier enums,
  positive finite budgets. Profiles are operator authority (AGENTS.md rule
  14): they live OUTSIDE the target repository — the repo-local
  `.loopforge/` directory is gitignored for exactly this purpose — and
  target-repo content can never supply or widen one.
- `entrypoints/repair.py`: `build_adopted_repair_runtime` gained
  caller-owned `environment` and `limits` parameters (the hardcoded
  `{"CIVICML_ENV": "test"}` injection and 2 GiB ceiling moved out to the
  wiring callers; defaults unchanged for existing callers).
- `entrypoints/cli.py`: new `loop` command with `--dry-run` (resolves and
  prints the profile without model or sandbox); `civicml-loop` retained and
  now passes its environment explicitly; stray positionals on non-`loop`
  commands still exit with a usage error.
- Local-only profiles (gitignored, not part of the repository content):
  `.loopforge/civicml.toml` (reproduces the hardcoded dogfood wiring),
  `.loopforge/loopforge-self.toml` (self-targeting, local mode, checks
  scoped to `tests/unit` + ruff + pyright + lint-imports),
  `.loopforge/README.md` (schema + authority rules + in-place-edit warning).
- Evidence: 1478 tests passing (18 platform-gated skips), 93.78% branch
  coverage, ruff format/check + pyright strict + import-linter all green;
  `--dry-run` verified against both profiles; allow/deny validation pinned
  by 26 new profile tests per rule 10; no network in unit tests (rule 9).
- Known limitation (documented in the profile README): the DeepSeek
  adapter's cost accounting uses pro-tier rates, so `deepseek-v4-flash`
  runs overestimate reported spend.

## Planned path to v1.0

- PACS-006 Context authority model
- PACS-007 Context lifecycle + prompt contracts
- PACS-008 Observability foundation
- PACS-009 Hardened container sandbox
- PACS-010 Software-repair workload + verifier stack
- PACS-011 First live model adapter
- PACS-012 Model capability registry + routing
- PACS-013 Orchestrator/worker + worktree isolation
- PACS-014 Evaluator + evidence-grounded Reflexion + async HITL
- PACS-015 Execution provenance graph + trajectory debugger
- PACS-016 Locked benchmark + multi-trial evaluation laboratory
- PACS-017 Adaptive context + shadow policies / v1.0

Exact objectives and acceptance gates are in `docs/process/remaining-pacs-plan.md`.

## Non-negotiable architecture rules

- model proposals never define their own authority, risk, retry, or approval requirements
- event history remains authoritative; state, telemetry, and provenance are projections
- safety/authorization policy is immutable during a run
- adaptive policy can optimize only within granted authority
- external side effects assume at-least-once execution unless an adapter proves stronger semantics
- ambiguous non-idempotent side effects fail closed
- context trust/provenance must never silently elevate untrusted/model-derived content to policy authority
- live model nondeterminism does not enter deterministic CI
- evaluator/reflection output never overrides deterministic verifier truth
- local sandbox limitations must not be overclaimed as hostile-code isolation

## GitHub bootstrap

At this checkpoint the local repository has no remote. The connected GitHub integration available to the previous agent could write to existing repositories but could not create a new repository, and no existing LoopForge repository was found.

Once an empty GitHub repository exists (recommended name: `loopforge`), push the local history without squashing it:

```bash
git remote add origin git@github.com:<owner>/loopforge.git
git branch -M main
git push -u origin main
```

If HTTPS auth is preferred, use the corresponding HTTPS remote. Verify the remote commit equals local `HEAD` before starting a new PACS cycle.

Do not reconstruct the project as a single GitHub commit; preserve the existing PACS-oriented history.
