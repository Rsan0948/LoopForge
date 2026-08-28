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

Completed: PACS-001 through PACS-010.

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
`docs/process/cycles/PACS-010-software-repair-workload.md`. Checkpoint **not yet committed**
(awaiting operator confirmation).

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

Current checked evidence (PACS-010 + post-cycle hardening pass, 2026-08-27, Docker Desktop
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
- `docs/process/cycles/PACS-010-software-repair-workload.md`

## Next authorized work

None. PACS-011 through PACS-017 are PLANNED, not active.

The PACS-010 checkpoint is reconciled but **uncommitted**; the operator must confirm the commit
and manually initiate PACS-011 (first live model adapter) or another explicitly named scope.

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
