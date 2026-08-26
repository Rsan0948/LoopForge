# Build status — PACS-006 complete

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
