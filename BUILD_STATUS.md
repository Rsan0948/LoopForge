# Build status — PACS-010 complete

PACS-010 establishes the software-repair reference workload and deterministic verifier stack
while keeping the runtime workload-agnostic. New ports (`WorkspaceManagerPort`,
`RunArtifactPort`) bind a Git-backed workspace manager (status/diff/checkout, symlink-safe
untracked diff rendering), safe file read/search/edit tools over the sandbox file API, and a
composite tool executor. The `workloads/` package (new import-linter layer between `ports` and
`application`) binds repair tools that declare code-owned
`SandboxRequirements(process_filesystem_isolated=True, network_isolated=True)` — binding fails
closed anywhere `ContainerSandbox` is unavailable — and composes a deterministic
`RepairVerifier` (predefined sandbox commands + patch constraints + contract-checked
acceptance hooks); verifier truth is code-owned and model output never overrides it. Fixture
repository content enters model context only as `TrustClass.UNTRUSTED_CONTENT` (rule 16).
Workspace snapshot/diff evidence persists as the 19th domain event `ArtifactRecorded` (schema
v1 unchanged): durable, replayable through the codec, projected to telemetry as metadata only.
A `repair-demo [--container IMAGE]` CLI wires the full stack; the live container run repairs
the adder-regression fixture in 2 iterations with the exact patch recorded. See
`docs/process/cycles/PACS-010-software-repair-workload.md` (realizes ADR-0005 on ADR-0008/0009).
No subsequent PACS cycle is active until manually initiated.

Verified in this environment (2026-08-27, Docker Desktop 29.2.1 live,
`python:3.12-alpine` pulled; includes post-cycle hardening pass):

- `uv run pytest -q --cov` — **1237 passing, 15 skipped** (skips are exactly the macOS
  `RLIMIT_AS` platform gates — 9 pre-existing local-sandbox + 4 trusted-local repair E2E + 1
  repair-demo CLI smoke — plus 1 non-UTF-8-filesystem gate; **zero** docker-gated skips — all
  13 live container isolation tests and the live container repair E2E executed against the
  real runtime)
- branch-aware coverage — **96.44% overall**; configured 90% gate satisfied
- `ruff format --check .` / `ruff check .` — clean (126 files)
- `pyright` (strict) — 0 errors, 0 warnings
- `lint-imports` — 2 contracts kept, 0 broken (layers now include `workloads`)
- live CLI evidence — `repair-demo --container python:3.12-alpine` → `status=succeeded
  iterations=2`, verifier `command:run_tests: passed (exit_code=0); patch_constraints: passed
  (files changed: adder.py)`, exact unified diff recorded (`return left - right` →
  `return left + right`)

Discoveries fixed and pinned this cycle: fixture commands run with `python -B` (same-second
edits were defeated by stale `__pycache__` bytecode, silently failing verification); Docker
Desktop's containerd image store fails short-name `docker image inspect` resolution, so both
live docker probes (PACS-009 and PACS-010) fall back to the canonical fully-qualified reference
— probe-only change, `docker run` argv untouched.

Post-cycle hardening fixed and pinned (operator-initiated; see
`tests/regression/test_hardening_regressions.py` PACS-010 section, the unit suites named in
the cycle record, and `tests/security/test_local_sandbox.py`): hostile repository content
could drive host-side Git execution — a model-planted `.git/config` textconv written through
the rw bind mount would execute on the next host `git diff` — so the workspace adapter now
verifies a materialize-time metadata fingerprint before every host Git invocation and runs
Git with a hermetic environment (the file API also rejects `.git` paths); ignored files
(`__pycache__/`) were invisible to `status()` and survived `reset()` (now reported as
untracked deviations and reclaimed by `clean -fdqx`); `checkout()` reverted from the index
with glob-able pathspecs (now reverts from the base revision with `:(literal)` pathspecs);
non-UTF-8 filenames crashed output decoding and control-character filenames could forge lines
in the rendered evidence document; non-string tool arguments crashed with `AttributeError`;
verifier check-hook exceptions crashed the run instead of failing closed; hostile filenames
could inject forged verifier-verdict lines into summaries/context/telemetry (detail strings
now escaped and bounded); vacuous acceptance criteria were satisfiable by doing nothing;
artifact-collector exceptions permanently wedged runs in VERIFYING (now a terminal
`ARTIFACT_COLLECTION_FAILED` stop, plus a guard for the double-stop transition it exposed);
crash/resume between artifact appends could duplicate byte-identical evidence (deduplicated
by a `(kind, label, content)` fingerprint in `RunState.recorded_artifacts`); a NaN
`VerificationFailed` score encoded into an undecodable event stream (finite scores now
enforced at the domain and port boundaries); image names starting with `-` were interpolated
into the `docker run` argv as flags; and `repair-demo --container ""` leaked an uncaught
traceback (now exit 2 with a clean error, sandbox destroyed in `try/finally`).

## Implemented through PACS-010

- `domain/workspace.py` / `domain/artifacts.py` / `domain/verification.py`: workspace status,
  diff and patch-constraint vocabulary (pure-string paths, no `pathlib` in domain),
  `ArtifactKind`, `CheckOutcome` composition
- 19th event `ArtifactRecorded` through all four touchpoints: `Event` union, reducer arm +
  `_ALLOWED_STATUS`, JSON codec registry/constructor/dispatch, telemetry projector arm
  (kind/label/content_bytes only — evidence never enters telemetry)
- `ports/workspace.py` (`WorkspaceManagerPort`), `ports/artifacts.py` (`RunArtifactPort`,
  `ArtifactContractError`); `Runtime` optional artifact seam records evidence after
  verification, before terminal stop — runtime stays workload-agnostic
- `adapters/git_workspace.py` (`GitWorkspaceManager`: offline hermetic fixture repos,
  fail-loud rematerialization), `adapters/file_tools.py` (exact-match edit with
  occurrence-count validation), `adapters/workspace_git_tools.py`,
  `adapters/composite_tools.py`
- `workloads/repair.py` + `workloads/fixtures.py`: tool/context/verifier binding,
  adder-regression deterministic task, scripted repair actions for E2E
- `entrypoints/repair.py` composition root + `repair-demo [--container IMAGE]` CLI
- test suites: `test_workspace`, `test_file_tools`, `test_git_workspace`,
  `test_workspace_tools`, `test_repair_verifier`, `test_repair_workload`,
  `test_runtime_artifacts`, integration `test_repair_runtime` (scripted-model E2E incl. live
  container variant), security `test_repair_isolation`, event-catalog updates (18→19)

# Historical: PACS-009 complete

PACS-009 establishes the hardened container sandbox: `ContainerSandbox`, a `SandboxPort` adapter
that executes allowlisted commands inside hardened Docker containers via the Docker CLI (zero new
dependencies, auditable argv). Workloads run with only the workspace bind-mounted (`/workspace`),
a read-only rootfs, size-bounded noexec/nosuid tmpfs, deny-all networking (`ContainerNetworkPolicy`
`NONE`), dropped capabilities, `no-new-privileges`, memory/swap/pids/CPU/nofile limits, explicit
`--env` filtering (host environment never inherited), bounded captured output, and wall-clock
timeouts that kill the named container — destroying its PID namespace so no child workload
survives; `destroy()`/context-manager exit guarantees the same. The file API composes
`ConstrainedLocalSandbox`, retaining its traversal/symlink/byte-limit defenses unchanged. The
adapter truthfully advertises `process_filesystem_isolated=True` and `network_isolated=True`
(enforced on every run) and keeps `kernel_isolated=False` (shared-kernel containers; no VM-grade
claim), so network-isolation workloads bind here but not to the local adapter, and
kernel-isolation requirements fail closed against both. Live isolation evidence is
capability-gated (`_REQUIRES_DOCKER` probe with reason-coded skips), keeping deterministic CI
hermetic without a daemon. See `docs/process/cycles/PACS-009-hardened-container-sandbox.md` and
ADR-0009. No subsequent PACS cycle is active until manually initiated.

Verified in this environment (2026-08-27, Docker Desktop 29.2.1 live; includes post-cycle
hardening pass):

- `uv run pytest -q --cov` — **1091 passing, 9 skipped** (skips are the pre-existing macOS
  `RLIMIT_AS` platform gates; all 13 live container tests executed against the real runtime)
- branch-aware coverage — **97.86% overall**; configured 90% gate satisfied;
  `adapters/container_sandbox.py` at 100% branch coverage
- hermetic-CI proof — with no `docker` on `PATH`: 1078 passing, 22 reason-coded skips
- `ruff format --check .` / `ruff check .` — clean (103 files)
- `pyright` (strict) — 0 errors, 0 warnings
- `lint-imports` — 2 contracts kept, 0 broken
- deterministic CLI demo — `status=succeeded` with the correlated telemetry narrative
- `compileall` — clean

Post-cycle hardening fixed and pinned (operator-initiated; see
`tests/regression/test_hardening_regressions.py` and `tests/security/test_container_sandbox.py`):
missing/unexecutable Docker binary leaked a raw `OSError` instead of `SandboxError`; a wall
timeout firing during container startup could miss a single-shot `docker kill` and leave the
workload running unsupervised (kill is now retried on a bounded deadline until the container
dies or the CLI exits); a comma in the resolved workspace root silently corrupted `--mount`
bind parsing (now rejected); and non-finite (NaN/Infinity) values bypassed `<= 0` validation in
`CommandSpec`, `SandboxLimits`, `ContainerSandboxConfig`, and both adapters' runtime timeout
(Infinity would have silently disabled the wall timeout) — the local adapter also now validates
the runtime timeout before spawning instead of after.

## Implemented through PACS-009

- `ContainerSandbox` + frozen validated `ContainerSandboxConfig` (image/allowlist/environment/
  network policy/resource ceilings are bootstrap authority; invalid configs unconstructable;
  injection-safe environment variable names enforced)
- hardened `docker run` argv: `--rm --init --pull never --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges --log-driver none`, workspace-only bind mount, bounded tmpfs,
  memory/swap/pids/CPU/nofile limits, sorted explicit `--env` pairs
- structural cleanup: timeout kills the named container (PID namespace destroyed), stubborn CLI
  process group SIGKILLed as client-side fallback; `destroy()`/`__exit__` kill in-flight
  containers; Docker CLI failures (exit 125 + `docker:` marker) surface as `SandboxError`, never
  confusable with workload exit codes
- capability negotiation proven in both directions: fail-closed binding for network/filesystem
  isolation against the local adapter, fail-closed kernel-isolation requirements against the
  container adapter; `TelemetrySandbox` wrapping preserves the stronger capability report
- `tests/security/test_container_sandbox.py` — 50 tests: constructor/config validation, exact
  capability profile, file-API defenses, deterministic argv pin, daemon-free plumbing fakes
  (timeout→kill, CLI failure, destroy, stubborn-CLI fallback), and 13 live gated tests
  demonstrating escape resistance, network denial, environment filtering, external
  timeout/CPU/pids/memory/tmpfs enforcement, bounded output, exit-code mapping, missing-image
  failure, and no-survivor destruction

# Historical: PACS-008 complete

PACS-008 establishes the observability foundation: an OpenTelemetry-compatible telemetry
vocabulary (`Span`/`LogRecord`/`MetricSample`, closed `SpanName`/`MetricName` vocabularies,
`CorrelationIds` for run/worker/cycle/action/tool/attempt/verification), a `TelemetryPort`
protocol with a fail-safe emission boundary, and runtime instrumentation that projects spans
for policy decisions, context builds, model turns, tool execution, verification, persistence,
retry, and the root run — plus structured logs and metrics for runs, duration, outcomes,
cycles, retries, circuits, stalls, budget stops, tool failures, verification failures, context
size/compaction, approvals, and token/cost/cache usage. Redaction is code-owned and happens
before emission (SENSITIVE/SECRET → `[redacted]`, fail-closed for unknown sensitivity); the
event store remains the only authoritative history and telemetry never feeds back into runtime
state or decisions; adapter failures are swallowed and counted, never corrupting run state.
No subsequent PACS cycle is active until manually initiated.

Verified in this environment (2026-08-27):

- `uv run pytest -q --cov` — **1013 passing, 9 skipped** (macOS `RLIMIT_AS` platform-gated skips)
- branch-aware coverage — **97.73% overall**; configured 90% gate satisfied; `domain/telemetry.py`,
  `ports/telemetry.py`, and `adapters/telemetry.py` at 100% branch coverage
- `ruff format --check .` / `ruff check .` — clean
- `pyright` (strict) — 0 errors, 0 warnings
- `lint-imports` — 2 contracts kept, 0 broken
- deterministic CLI demo — `status=succeeded` plus a causally correlated trace/log/metric
  narrative with `[redacted]` on the sensitive tool observation
- `compileall` — clean

## Implemented through PACS-008

- OTel-compatible telemetry data model with zero new dependencies; OTLP/JSON-shaped converters
  (`to_otlp_span`/`to_otlp_metric`/`to_otlp_log`) prove wire compatibility offline and reject
  unredacted `SensitiveText`
- `TelemetryPort` + `FailSafeTelemetry` boundary: pre-emission redaction, exception
  containment with a `dropped_records` counter; `NoOpTelemetry` and deterministic
  `InMemoryTelemetry` adapters
- deterministic per-run traces: trace id = run id, root run span emitted once at terminal stop
  with stop-reason-derived status, cycle spans parenting operation spans, per-run monotonic
  span ids; verification correlation ids derived from the authoritative event sequence
- event projector mapping all 18 domain event types (19 as of PACS-010) to structured logs and the full required
  metric set; context size/compaction gauges/counters from the
  `ContextAccountingSource.last_accounting` seam (PACS-007)
- `TelemetrySandbox` decorator emitting sandbox-execution spans (fail-safe, standalone or
  run-correlated)
- CLI demo narrative (`format_telemetry_narrative`) demonstrating causal correlation and
  pre-export redaction against the authoritative event store

# Historical: PACS-007 complete

PACS-007 establishes the context lifecycle and prompt contracts: deterministic context selection
with a hard token budget, explicit per-item budget accounting, preservation contracts for
objective/blockers/verifier failures/confirmed facts/pending approvals/irreversible actions,
structured compaction that may drop or truncate content but never alters trust or provenance,
role-specific assembly (`ModelRole`), and versioned prompt templates whose id/version are
recorded on every durable `ContextAssembled` event. Trust authority from PACS-006 is unchanged:
no new elevation path exists, and SECRET content still never persists. No subsequent PACS cycle
is active until manually initiated.

Verified in this environment (2026-08-27, includes post-cycle hardening pass):

- `uv run pytest -q` — **897 passing, 9 skipped** (macOS `RLIMIT_AS` platform-gated skips)
- branch-aware coverage — **97% overall**; configured 90% gate satisfied; all context, prompt,
  and codec modules at 100% branch coverage
- `ruff format --check .` / `ruff check .` — clean
- `pyright` (strict) — 0 errors, 0 warnings
- `lint-imports` — 2 contracts kept, 0 broken
- deterministic CLI demo — `status=succeeded` through `BudgetedContextBuilder`
- `compileall` — clean

Post-cycle hardening fixed and pinned (see `tests/regression/test_hardening_regressions.py`):
runtime rejection of context assembled for a different run, negative token-count validation at
the measurement point, stale-ledger clearing after failed builds, and a `truncate_content`
boundary-contract fix — plus edge pins for drop-reason precedence, supersession cycles,
role-scoped supersession, exact-fit boundaries, and metadata immutability.

## Implemented through PACS-007

- `select_context` deterministic policy: role scoping → supersession/freshness pruning →
  whole-or-fail preserved allocation → trust/recency-ranked greedy fill with marker truncation
- explicit context-budget accounting (`ContextAccounting` ledger; over-budget ledgers are
  unconstructable; template static text charged as overhead)
- `PreservationClass` contracts; compaction drops/truncates content only, never elevates or
  demotes trust; dangling supersession edges pruned as bookkeeping
- `ModelRole` (controller/planner/reflector) role-specific assembly
- versioned `PromptTemplate` / `RenderedPrompt` artifacts with construction-enforced
  stable-prefix layout (provider-independent, cache-friendly)
- `ContextBuilderPort.build_context(state, role, token_budget)` + `TokenCounterPort`;
  `BudgetedContextBuilder` reference adapter with a `last_accounting` ledger seam for telemetry
- `ContextAssembled` carries optional prompt template id/version as execution metadata
  (schema-v1 backward compatible: legacy payloads decode to null)

# Historical: PACS-006 complete

PACS-006 establishes the context authority model: typed, provenance-aware `ContextItem` /
`ModelContext` artifacts, code-owned trust/authority ordering with a guarded `promote` path, the
`ContextBuilderPort` boundary, and a durable `ContextAssembled` event so the exact model context
survives serialization and replay. The model boundary now receives `ModelContext`, never raw
`RunState`. No subsequent PACS cycle is active until manually initiated.

Verified in this environment (2026-08-25, includes post-cycle hardening pass):

- `uv run pytest -q` — **805 passing, 9 skipped** (macOS `RLIMIT_AS` platform-gated skips)
- branch-aware coverage — **96.31% overall**; configured 90% gate satisfied; all context
  modules and the `ContextAssembled` codec paths at 100% branch coverage
- `ruff format --check .` / `ruff check .` — clean
- `pyright` (strict) — 0 errors, 0 warnings
- `lint-imports` — 2 contracts kept, 0 broken
- deterministic CLI demo — `status=succeeded`
- `compileall` — clean

Post-cycle hardening fixed and pinned (see `tests/regression/test_hardening_regressions.py`):
snapshot re-validation at the serialization boundary, supersession-cycle rejection, and honest
"unknown" verifier-outcome labeling.

## Implemented through PACS-006

- typed immutable context artifacts with provenance (`ContextSource`), trust (`TrustClass`),
  sensitivity (`DataSensitivity`), supersession, and freshness semantics
- `TRUST_AUTHORITY` ordering and guarded `promote`: untrusted/model-inference content can never
  elevate to runtime-policy or authorized-human authority, and only ever with an explicit basis
- `ContextBuilderPort` boundary + deterministic `BasicContextBuilder` reference adapter
- durable `ContextAssembled` event (strict codec, SECRET-sensitivity persistence rejected)
- model boundary consumes `ModelContext`; builder contract violations fail with
  `ContextContractError`

# Historical: PACS-005 complete + restoration/hygiene pass green

PACS-005 establishes LoopForge's explicit security/trust vocabulary, provider-independent sandbox
contract, capability negotiation, and first constrained local execution adapter. No subsequent PACS
cycle is active until manually initiated.

## Restoration + hygiene pass (2026-08-23, operator-initiated, not a PACS cycle)

After the repository was restored from a damaged import bundle (see HANDOFF.md), a full
hygiene/hardening/test-rebuild pass was executed and every configured quality gate was run and
verified in this environment:

- `uv run pytest -q` — **696 passing, 9 skipped** (skips are macOS `RLIMIT_AS` platform limits,
  individually reason-coded)
- branch-aware coverage — **95.75% overall**; configured 90% gate satisfied
- `ruff format --check .` — clean (81 files)
- `ruff check .` — clean (0 findings; full configured ruleset incl. EM/TRY/PL)
- `pyright` (strict mode) — **0 errors, 0 warnings**
- `lint-imports` — 2 architecture contracts kept, 0 broken
- deterministic CLI demo — `status=succeeded`
- `compileall` — clean

Hardening fixes landed and pinned by `tests/regression/test_hardening_regressions.py`:

- non-finite (NaN/Infinity) values rejected in budgets, usage, timeouts, retry delays, and scores
- codec strict-schema fixes: boolean `schema_version`/`score` rejected, non-string `caused_by`
  rejected, non-string `failure_class` rejected instead of falling into the legacy decode path
- sandbox launcher (`_sandbox_exec`) failure protocol: rlimit/exec failures exit 97 with a stderr
  marker and surface as `SandboxError`, never confusable with workload exit codes
- unreachable TRANSIENT_ONLY retry guard removed; surviving reason codes pinned by tests

Test suites rebuilt after the bundle loss: `tests/unit/` (domain + adapters + codec + CLI),
`tests/security/` (sandbox traversal/symlink/environment/allowlist/timeout/limits/output and
capability-binding), `tests/resilience/` (fault-injection laboratory),
`tests/regression/` (hardening pins).

## Implemented through PACS-005 (historical)

- deterministic event-sourced runtime and SQLite compare-and-append persistence
- runtime-owned retry/backoff/idempotency, action journal, and tool-scoped circuit breaking
- deterministic no-progress/resource governance/cancellation
- reusable adversarial fault-injection laboratory and provider-boundary validation
- trust/authority taxonomy for later provenance-aware context work
- `SandboxPort`, machine-readable `SandboxCapabilities`, and code-owned `SandboxRequirements`
- constrained local file API with traversal/symlink defense and bounded atomic reads/writes
- fixed absolute argv command allowlist, `shell=False`, and explicit child environment
- wall-clock timeout/process-group kill plus POSIX CPU/address-space/open-file/file-size limits
- bounded process output
- sandbox tool binding that fails closed when required capabilities are unavailable
- explicit documentation that the local adapter does not provide process-filesystem, network, or
  kernel isolation

## Verified in this build environment (original PACS-005 build, pre-restoration)

- `PYTHONPATH=src python -m pytest -q` — 125 passing (historical record; the current tree runs
  696 passing — see the restoration section above)
- branch-aware coverage — 93% overall; configured 90% gate satisfied
- security-specific suite — 22 passing
- path traversal/absolute path rejection — passing
- symlink escape rejection — passing
- host-secret environment non-inheritance — passing
- command allowlist/no-shell execution contract — passing
- wall-clock timeout/process-group termination — passing
- resource/output/file-size limits — passing
- unsupported sandbox capability requirement fails before tool execution — passing
- compile/demo/architecture/diff/line-length checks — passing

## Security boundary

`ConstrainedLocalSandbox` is a constrained local development/reference adapter, not hostile-code
isolation. It reports `process_filesystem_isolated=False`, `network_isolated=False`, and
`kernel_isolated=False`. A later container/VM adapter must provide those properties before LoopForge
uses them for untrusted executable repositories.

## External quality tools

`ruff`, `pyright`, and `lint-imports` were unavailable in the original build environment. As of the
2026-08-23 restoration/hygiene pass they run in CI and locally, and all three are green.
