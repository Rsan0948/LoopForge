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

Completed: PACS-001 through PACS-007.

PACS-007 (context lifecycle and prompt contracts, 2026-08-27) added deterministic context
selection with hard token budgeting, explicit per-item accounting, preservation contracts,
structured compaction/pruning, role-specific assembly (`ModelRole`), and versioned prompt
templates whose id/version are persisted on every `ContextAssembled` event. See
`docs/process/cycles/PACS-007-context-lifecycle-and-prompt-contracts.md`.

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

Current checked evidence (PACS-007 + post-cycle hardening pass, 2026-08-27):
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
- `docs/process/cycles/PACS-007-context-lifecycle-and-prompt-contracts.md`

## Next authorized work

None. PACS-008 through PACS-017 are PLANNED, not active.

The operator must manually initiate PACS-008 or another explicitly named scope.

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
