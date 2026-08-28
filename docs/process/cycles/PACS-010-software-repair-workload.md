# PACS-010 — Software-repair workload and deterministic verifier stack

Status: SUCCESS

## Objective

Turn software repair into the reference workload while keeping the runtime itself
workload-agnostic.

## Why now

PACS-007 delivered context lifecycle/prompt contracts (fixture content can enter model context
with explicit trust), PACS-008 delivered the observability projection (repair runs join the trace
for free), and PACS-009 delivered the hardened container sandbox with enforced
`process_filesystem_isolated`/`network_isolated` capabilities — the boundary the threat model
requires before untrusted repository content may be executed. All remaining seams (runtime,
ports, policy, events) were workload-agnostic; what was missing was the workload itself.

## Dependencies

- PACS-007 (SUCCESS): context authority/lifecycle; `TrustClass.UNTRUSTED_CONTENT` plumbing.
- PACS-009 (SUCCESS): `ContainerSandbox` enforcing the capabilities repair tools require.

## In scope

- repository/workspace manager port (`WorkspaceManagerPort`) and a Git-backed adapter
  (`GitWorkspaceManager`) with status/diff/checkout primitives;
- safe file read/search/edit tools over the `SandboxPort` file API (traversal/symlink/byte-limit
  defenses inherited by composition);
- predefined test/lint/type/build commands through the sandbox (`SandboxCommandTools`);
- deterministic verifier composition (`RepairVerifier`: sandbox command outcomes + patch
  constraints) with acceptance-criteria hooks (`RepairCheck`), contract-checked at the boundary;
- workspace snapshot/diff artifacts persisted through a new workload-agnostic
  `RunArtifactPort` seam as durable, replayable `ArtifactRecorded` events (19th event type);
- fixture repositories for deterministic repair tasks (hermetic, offline, `git init` only);
- a `repair-demo` CLI entrypoint with `--container IMAGE` for live isolated execution.

## Out of scope

- live models (PACS-011);
- multi-file/multi-fixture benchmark corpora (PACS-016);
- evaluator/reflection integration (PACS-014);
- any new isolation claim: repair tools declare code-owned `SandboxRequirements` and binding
  fails closed anywhere the required capabilities are absent (rules 13-15);
- schema-version bump: the new event type rides schema v1.

## Plan

1. Domain vocabulary: `domain/workspace.py` (`WorkspaceStatus`, `WorkspaceDiff`,
   `PatchConstraints`, `AcceptanceCriteria`, pure-string path validation — no `pathlib` in
   domain), `domain/artifacts.py` (`ArtifactKind`), `domain/verification.py`
   (`CheckOutcome` composition), and the 19th event `ArtifactRecorded` wired through the four
   required touchpoints: `Event` union, `_ALLOWED_STATUS` + `reduce_event` arm, JSON codec
   registry/constructor/dispatch, telemetry projector arm (metadata only; artifact content
   never enters telemetry).
2. Ports: `ports/workspace.py` (`WorkspaceManagerPort`), `ports/artifacts.py`
   (`RunArtifactPort`, `RunArtifact`, `ArtifactContractError`); `Runtime` gains an optional
   artifact collector seam — the runtime stays workload-agnostic and records whatever the
   workload binding collects, after verification, before terminal stop.
3. Adapters: `git_workspace.py` (`GitWorkspaceManager`/`GitWorkspace`, symlink-safe untracked
   diff renderer), `file_tools.py` (read/search/write/edit over the sandbox file API,
   exact-match edit with occurrence-count validation), `workspace_git_tools.py`
   (`GitWorkspaceTools`), `composite_tools.py` (executor composition).
4. Workload: `workloads/repair.py` binds tools, context (fixture content enters only as
   `TrustClass.UNTRUSTED_CONTENT`, rule 16), and the deterministic verifier; untrusted repair
   tools declare `SandboxRequirements(process_filesystem_isolated=True, network_isolated=True)`
   so binding fails closed without `ContainerSandbox`. `workloads/fixtures.py` defines the
   deterministic adder-regression task.
5. Entrypoint: `entrypoints/repair.py` composition root + `repair-demo [--container IMAGE]`
   CLI; trusted-local path remains honestly gated on the platform sandbox.
6. Tests: unit suites for every new module, event-catalog updates (18→19), integration E2E
   (scripted model through the full runtime), security isolation suite, live container run
   gated by a `_REQUIRES_CONTAINER` probe (mirroring the `RLIMIT_AS`/`_REQUIRES_DOCKER`
   pattern).
7. Gates: `ruff format --check`, `ruff check`, `pyright` (strict), `lint-imports`,
   `pytest --cov` with branch coverage ≥ 90%.

## Act

Implemented as planned. Notable decisions and discoveries:

- **The runtime stayed workload-agnostic; the workload binds through ports.** `Runtime` gained
  exactly one optional seam (`artifacts: RunArtifactPort | None`). All repair semantics —
  workspace provisioning, tool binding, verifier composition, fixture trust labeling — live in
  `workloads/`, which the import-linter layers contract and the dependency-DAG test now place
  between `ports` and `application` (both contracts kept).
- **Verifier truth is deterministic and code-owned.** `RepairVerifier` composes sandbox command
  outcomes (`command:run_tests: passed (exit_code=0)`) with patch constraints
  (`patch_constraints: passed (files changed: adder.py)`) and contract-checked
  acceptance hooks; a hook returning anything but `CheckOutcome` raises
  `RepairCheckContractError`. Model output never overrides the verdict — the false-success
  rejection test pins a run whose scripted model declares success while tests still fail.
- **Trust discipline held end-to-end (rule 16).** Fixture repository content enters model
  context only as `TrustClass.UNTRUSTED_CONTENT`; tool metadata remains code-owned (rule 4);
  untrusted repair tools bind only where `process_filesystem_isolated` and `network_isolated`
  are truthfully advertised, which today means `ContainerSandbox` only — construction against
  `ConstrainedLocalSandbox` fails closed (pinned by `tests/security/test_repair_isolation.py`).
- **Artifacts are durable events, not side files.** Workspace snapshots and diffs are recorded
  as `ArtifactRecorded` events through the codec with full replay support (`replay()` rebuilds
  artifact state); telemetry projects kind/label/content_bytes only, so evidence payloads never
  enter the telemetry stream. Schema stays v1.
- **`python -B` determinism fix.** Fixture test commands initially ran plain `python`; when the
  edit tool rewrote `adder.py` within the same mtime second, stale `__pycache__` bytecode made
  the fixed code test as still-broken (the scripted model then "exhausted" without the verifier
  ever passing). All fixture command argvs now use `python -B`. This was the root cause of the
  only mid-cycle E2E failure.
- **Docker image-probe quirk discovered and hardened.** Docker Desktop 29.2.1's containerd
  image store fails short-name resolution in `docker image inspect python:3.12-alpine`
  ("No such image") while `docker run` resolves it fine; the canonical fully-qualified
  reference (`docker.io/library/...`) is reliable. Both live probes (PACS-010's
  `_container_ready` and PACS-009's `_docker_ready`) now try the short name and fall back to
  the fully-qualified form. This is a test-probe change only; the adapter's `docker run` argv
  is untouched.
- **Sandbox selection is explicit and honest.** `--container IMAGE` wires `ContainerSandbox`
  (the acceptance path in this environment); the default trusted-local path uses
  `ConstrainedLocalSandbox` and is platform-gated exactly like the pre-existing nine
  `RLIMIT_AS` skips on this macOS kernel (launcher fails closed:
  "resource limits rejected") — a platform fact, not a cycle defect.

## Check

Executed in this environment (2026-08-27, uv-managed toolchain, Docker Desktop 29.2.1 live,
`python:3.12-alpine` pulled):

- `uv run pytest -q --cov` — **1186 passing, 14 skipped** (skips are exactly the macOS
  `RLIMIT_AS` platform gates: 9 pre-existing local-sandbox, 4 trusted-local repair E2E, 1
  repair-demo CLI smoke; **zero** docker-gated skips — all 13 live container isolation tests
  and the live container repair E2E executed against the real runtime), up from 1091 passing
  at PACS-009
- branch-aware coverage — **97.03% overall**; configured 90% gate satisfied
- `ruff format --check .` — clean (126 files); `ruff check .` — clean (0 findings)
- `pyright` (strict mode) — 0 errors, 0 warnings
- `lint-imports` — 2 architecture contracts kept, 0 broken (layers now include `workloads`)
- live CLI evidence — `uv run python -m loopforge.entrypoints.cli repair-demo --container
  python:3.12-alpine` prints `status=succeeded iterations=2 cost=$0.02`, verifier summary
  `command:run_tests: passed (exit_code=0); patch_constraints: passed (files changed:
  adder.py)`, `workspace_id=adder-regression`, the base revision, and the exact unified diff
  (`return left - right` → `return left + right`), with 2 `workspace_snapshot` artifacts
  recorded

Acceptance-gate evidence, criterion by criterion:

- *a scripted model can repair a fixture repository through the full runtime* —
  `tests/integration/test_repair_runtime.py` success-path E2E (trusted-local and live container
  variants): scripted model edits `adder.py`, predefined `run_tests` command passes inside the
  sandbox, run terminates `SUCCEEDED` after two iterations.
- *success is granted only after independent verifier approval* — the same E2E asserts the
  terminal state carries `VerificationPassed` derived from the `RepairVerifier` composition
  (sandbox command outcome + patch constraints), never from model claims; unit suites
  (`test_repair_verifier.py`, `test_repair_workload.py`) pin composition and contract checking.
- *false-success attempts are rejected* — the E2E false-success test drives a scripted model
  that declares success while the tests still fail; the verifier rejects, the run does not
  succeed, and the rejection is durable in the event history.
- *repository changes remain confined to the assigned workspace* —
  `tests/security/test_repair_isolation.py` (workspace confinement, traversal/symlink
  rejection through the inherited file-API defenses, fail-closed capability binding) plus the
  PACS-009 container escape-resistance suite the repair run executes within.
- *a replayable run captures the exact patch and verification evidence* — the E2E reopens the
  SQLite store, replays the event history, and asserts the recorded `ArtifactRecorded`
  workspace snapshots and diff contain the exact patch bytes and the verifier summary; the
  JSON codec round-trips all 19 event types with schema v1.

Post-cycle hardening pass (operator-initiated, 2026-08-27, after the SUCCESS classification):
a three-agent adversarial review of the cycle's own work triaged ~25 findings; every
actionable defect was verified against the real code, fixed, and pinned —

1. **hostile repository content could drive host-side Git execution (most severe):** untrusted
   container code rewrites `.git/config` through the rw bind mount, so the next host-side
   `git diff` would execute a model-planted textconv shell command. The workspace adapter now
   fingerprints `.git/config` + `.git/info/attributes` at materialization and refuses every
   host Git invocation once metadata changes, runs Git with a hermetic environment (fixed
   PATH, `GIT_CONFIG_NOSYSTEM`, no global config, no terminal prompts), and the sandbox file
   API independently rejects any `.git` path component;
2. **verifier/evidence blind spots in workspace status:** ignored files (`__pycache__/`) were
   invisible to `status()` and survived `reset()` — status now reports ignored files as
   untracked deviations (`--ignored=matching`), and reset uses `clean -fdqx`. `checkout()`
   reverted from the index and glob pathspecs could fan out (`*.py`); it now reverts from the
   base revision with `:(literal)` pathspecs. Non-UTF-8 filenames crashed output decoding
   (now `surrogateescape`) and control-character filenames could forge lines inside the
   rendered evidence document (now refused verbatim);
3. **tool/adapter contract gaps under hostile input:** non-string tool arguments crashed with
   `AttributeError` (now `TOOL_ARGUMENTS` failures), unreadable or binary files crashed search
   (now skipped), and non-UTF-8 reads / write `OSError`s escaped the sandbox contract (now
   `SandboxError`; path violations map to a permanent `SANDBOX_PATH` error code);
4. **verifier fail-open edges:** check-hook exceptions crashed the run instead of failing
   closed (now recorded as failed outcomes), workspace errors during patch evaluation became
   crashes (now failed outcomes), model-planted hostile filenames could inject forged
   verifier-verdict lines into summaries, context, and telemetry (detail strings are now
   escaped and bounded), and vacuous acceptance criteria (no required commands, no patch
   change) were satisfiable by doing nothing (rejected at `RepairTask` construction);
5. **runtime durability wedges:** an artifact-collector exception permanently wedged the run
   in VERIFYING (now a terminal `ARTIFACT_COLLECTION_FAILED` stop, with a terminal guard
   fixing the double-`RunStopped` transition it exposed), a crash/resume between artifact
   appends could duplicate byte-identical evidence (now deduplicated by a
   `(kind, label, content)` fingerprint projected into `RunState.recorded_artifacts`), and
   verifier port returns are boundary-validated like every other port return;
6. **domain-boundary validation:** artifact labels/content are validated at the
   `ArtifactRecorded` event boundary (blank/control-character labels, content over the
   4 MiB budget), a NaN `VerificationFailed` score previously encoded into an undecodable
   event stream (finite scores enforced on the event, `CompositeVerification`, and the
   verifier port), `CheckOutcome.passed` requires a strict bool, workspace path/name
   validation rejects backslashes and control characters, and `max_changed_files` rejects
   bools/floats;
7. **container/CLI surfaces:** image names starting with `-` were interpolated into the
   `docker run` argv as flags (rejected at config construction), `RepairRuntimeBundle` now
   exposes the sandbox so its lifecycle is reachable, and `repair-demo --container ""`
   exits 2 with a clean stderr error (with the sandbox destroyed in a `try/finally`) instead
   of an uncaught traceback.

Three findings were documented as designed rather than fixed: `ScriptedModel` exhaustion
raising (a code-owned test/demo seam), the artifact sensitivity posture (evidence is
workspace-derived), and the artifact timing contract (evidence recorded after each
verification, not on cancel/budget paths).

Evidence: **1237 passing / 15 skipped** (the 15 skips are exactly the macOS `RLIMIT_AS`
platform gates — 9 local-sandbox, 4 trusted-local repair E2E, 1 CLI smoke — plus 1
non-UTF-8-filesystem gate; zero docker-gated skips), **96.44% branch coverage** (90% gate
satisfied), `ruff format --check`/`ruff check`/`pyright` (strict)/`lint-imports` all green.
Pins live in `tests/regression/test_hardening_regressions.py` (PACS-010 section), the unit
suites (`test_git_workspace.py`, `test_file_tools.py`, `test_workspace_tools.py`,
`test_repair_verifier.py`, `test_repair_workload.py`, `test_runtime_artifacts.py`,
`test_state.py`, `test_events.py`, `test_json_events.py`, `test_telemetry_projection.py`,
`test_container_sandbox.py`, `test_cli.py`), and `tests/security/test_local_sandbox.py`.

## Stop

**SUCCESS.** All five acceptance-gate criteria are demonstrated by executable evidence —
including a live, container-isolated end-to-end repair against the real Docker runtime — and
every quality gate is green with hermetic daemon-free CI preserved (docker-gated and
platform-gated tests skip with reason codes). The runtime remains workload-agnostic; no new
isolation is claimed; verifier truth stays deterministic and code-owned. No new ADR was
required: the cycle realizes ADR-0005 (software-repair reference workload) on top of
ADR-0008/0009 (capability contract, container adapter). Checkpoint not yet committed (awaiting
operator confirmation).

## Follow-on implications

- PACS-011 (first live model adapter) can target the repair workload immediately: the context
  builder already labels fixture content untrusted, the verifier stack is provider-independent,
  and live-model nondeterminism stays out of deterministic CI by the existing gating pattern.
- The `RunArtifactPort` seam is workload-agnostic: future workloads (evaluation, provenance)
  can record their own artifact kinds through the same durable, replayable path; new
  `ArtifactKind` members must extend the codec and telemetry projector arms deliberately.
- An egress-allowlist `ContainerNetworkPolicy` member (PACS-009 follow-on) would let repair
  tasks fetch dependencies; it must stay code-owned and re-justify `network_isolated`
  truthfulness before repair tools may bind with it.
- `kernel_isolated` remains unsatisfiable by design; repair workloads requiring VM-grade
  isolation will fail closed until such a backend exists.
