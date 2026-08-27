# PACS-009 — Hardened container sandbox

Status: SUCCESS

## Objective

Add an isolation backend suitable for untrusted repository/build execution and enforce
capabilities rather than merely documenting them.

## Why now

PACS-005 established the provider-independent `SandboxPort`, machine-readable
`SandboxCapabilities`, code-owned `SandboxRequirements`, and fail-closed tool binding, but its
only adapter (`ConstrainedLocalSandbox`) honestly reports
`process_filesystem_isolated=False`, `network_isolated=False`, and `kernel_isolated=False`.
The threat model defers hostile/untrusted repository execution to a container/VM adapter, and
PACS-010 (software-repair workload) depends on that boundary existing. PACS-008 delivered the
observability projection, so container execution can join the run trace via the existing
`TelemetrySandbox` seam with no new plumbing.

## Dependencies

- PACS-005 (SUCCESS): sandbox port, capability contract, negotiation, local adapter defenses.
- PACS-008 (SUCCESS): `TelemetrySandbox` decorator for optional, fail-safe span emission.

## In scope

- container-backed `SandboxPort` adapter (`ContainerSandbox`, Docker CLI, zero new dependencies);
- filesystem/process/network isolation capabilities truthfully advertised and actually enforced
  (mount namespace confinement to the workspace, PID namespace, `--network none`, read-only
  rootfs, dropped capabilities, `no-new-privileges`);
- explicit code-owned network policy (`ContainerNetworkPolicy`);
- resource limits (memory, pids, CPU seconds, open files, tmpfs) and process cleanup
  (named containers, kill on timeout, `destroy()` leaving no running child workload);
- environment/secret filtering (explicit environment only; host environment never inherited);
- path/symlink protections retained from the local adapter by composing
  `ConstrainedLocalSandbox` for the file API, unchanged;
- bounded captured output and wall-clock timeouts (runtime timeout only tightens);
- capability negotiation tests demonstrating fail-closed binding (network-requiring workloads
  cannot bind to the local adapter; kernel-isolation requirements fail against this adapter too);
- security test suite covering traversal, symlinks, environment leakage, process/resource
  abuse, and capability mismatch, with live-container tests capability-gated so deterministic
  CI stays hermetic without a daemon.

## Out of scope

- claiming VM/kernel-grade isolation beyond what the backend provides
  (`kernel_isolated` stays `False`: containers share a kernel with the runtime host; the
  Docker Desktop Linux VM is an implementation detail, not a contract guarantee);
- software-repair workload/verifier stack (PACS-010);
- live models (PACS-011);
- live OTel exporters (post-PACS-008 follow-on);
- egress allowlist/proxy network policies (only the deny-all policy is implemented).

## Plan

1. `adapters/container_sandbox.py`: `ContainerNetworkPolicy` (deny-all only), `ContainerSandbox`
   implementing `SandboxPort` by composing `ConstrainedLocalSandbox` for `read_text`/`write_text`
   and executing allowlisted `CommandSpec`s through `docker run` with hardened flags
   (`--rm --init --pull never --network none --read-only --cap-drop ALL
   --security-opt no-new-privileges --pids-limit --memory/--memory-swap --ulimit nofile/cpu
   --tmpfs --log-driver none`, workspace bind mount, explicit `--env` pairs). Wall-clock timeout
   kills the named container (destroying its PID namespace); Docker CLI failures (exit 125 with
   the `docker:` stderr marker) surface as `SandboxError`, never confusable with workload exit
   codes. `destroy()` kills any in-flight workload container.
2. Capability report: local-adapter capabilities plus `process_filesystem_isolated=True` and
   `network_isolated=True`; `kernel_isolated=False` (capability honesty, AGENTS.md rules 13–15).
3. Tests (`tests/security/test_container_sandbox.py`):
   - ungated: constructor validation, exact capability profile, file-API traversal/symlink/byte
     defenses (via composition), deterministic `docker run` argv shape, fail-closed capability
     negotiation against both adapters and through `TelemetrySandbox`, and plumbing tests with a
     faked `subprocess.Popen`/`subprocess.run` (timeout → container kill, CLI failure →
     `SandboxError`, `destroy()` semantics);
   - live, gated by a `_REQUIRES_DOCKER` probe (daemon + pinned test image, skip with reason
     codes like the `RLIMIT_AS` gates): hostile workloads cannot read/write outside the
     workspace mount, network is unreachable, host environment is invisible, timeout/resource
     limits are enforced by the container runtime externally, and destruction leaves no running
     container or child workload.
4. Docs: ADR-0009 (container adapter decision), `docs/architecture/sandbox-contract.md`,
   `docs/security/threat-model.md` capability table and residual risks, cycle record,
   BUILD_STATUS/HANDOFF reconciliation.
5. Gates: `ruff format --check`, `ruff check`, `pyright` (strict), `lint-imports`, `pytest --cov`
   with branch coverage ≥ 90%.

## Act

Implemented as planned. Notable decisions and discoveries:

- **`ContainerSandbox` composes, never reimplements, the local defenses.** `read_text`/`write_text`
  delegate to an internal `ConstrainedLocalSandbox` (constructed with the same root and limits), so
  the PACS-005 traversal/symlink/byte-limit protections are literally retained unchanged. The local
  adapter gained one additive read-only `root` property to support the composition; its behavior and
  capability report are untouched.
- **Config is a validated frozen dataclass.** `ContainerSandboxConfig` (image, command allowlist,
  environment, limits, network policy, pids/tmpfs ceilings, optional user, docker executable) fails
  at construction on blank/whitespace image references, non-positive limits, malformed users,
  invalid environment variable names (injection-safe `[A-Za-z_][A-Za-z0-9_]*`), and duplicate
  command names — invalid configs are unconstructable. All fields are bootstrap authority; neither
  repository nor model content can widen them (rule 14).
- **The enforcement surface is one auditable argv.** `docker run --rm --init --pull never
  --name loopforge-sandbox-* --network none --read-only --cap-drop ALL --security-opt
  no-new-privileges --log-driver none --pids-limit N --memory B --memory-swap B --ulimit nofile=S:S
  --ulimit cpu=S:S --tmpfs /tmp:rw,noexec,nosuid,size=B --workdir /workspace --mount
  type=bind,source=<root>,target=/workspace [--user u] --env K=V ... <image> <argv>` — pinned
  exactly by a deterministic argv test. `--pull never` guarantees no implicit registry access;
  environment pairs are sorted for determinism.
- **Cleanup is structural, not best-effort.** Timeout runs `docker kill` on the named container,
  which destroys the workload's PID namespace (subprocess-group kills cannot promise that); a
  stubborn CLI process group is then SIGKILLed as client-side belt-and-suspenders. `destroy()` and
  context-manager exit kill any in-flight container; `active_container` exposes in-flight state.
  Docker CLI failures (exit 125 + `docker:` stderr marker) raise `SandboxError("container runtime
  failed")` and can never be confused with workload exit codes — the same discipline as the
  PACS-005 launcher protocol.
- **Honesty in both directions.** The adapter advertises `process_filesystem_isolated=True` and
  `network_isolated=True` only because every run enforces them, and keeps `kernel_isolated=False`
  (shared-kernel containers; the Docker Desktop VM is not a contract guarantee). Negotiation tests
  prove a network/filesystem-isolation workload binds to this adapter but fails closed against the
  local adapter, and a kernel-isolation requirement fails closed against both.
- **Live evidence is capability-gated, CI stays hermetic.** A `_REQUIRES_DOCKER` probe (daemon +
  pinned `alpine:3.21` image) mirrors the `RLIMIT_AS` gate pattern: without a daemon the 13 live
  tests skip with reason codes and the 37 remaining tests pass in under a second. Plumbing paths
  (timeout → kill, CLI failure → `SandboxError`, destroy semantics, stubborn-CLI fallback) are
  covered daemon-free with a faked `subprocess.Popen`/`subprocess.run`.
- **Live-test calibration discoveries.** Fork storms under `--pids-limit` abort the shell with
  `can't fork` (exit 2) rather than queueing children; memory hogs die with exit 137 under the
  memory cgroup; Docker Desktop's VM exposes bind sources as VM paths (e.g.
  `/run/host_mark/private`), never host paths — assertions pin the actual enforcement signals.
- **Zero new dependencies; domain and ports untouched.** `SandboxRequirements`/`SandboxCapabilities`
  field sets are unchanged (field-equality tests still pin them); all negotiation, tool-binding,
  and telemetry wiring came free from the PACS-005/008 seams.

## Check

Executed in this environment (2026-08-27, uv-managed toolchain, Docker Desktop 29.2.1 live):

- `uv run pytest -q --cov` — **1063 passing, 9 skipped** (skips are the pre-existing macOS
  `RLIMIT_AS` platform gates; **zero** docker-gated skips with the daemon provisioned), up from
  1013 passing at PACS-008
- branch-aware coverage — **97.84% overall**; configured 90% gate satisfied;
  `adapters/container_sandbox.py` at **100% branch coverage**
- hermetic-CI proof — with `docker` absent from `PATH`: 37 passed + 13 reason-coded skips in
  0.15s for the container suite (daemon-free default remains green)
- `ruff format --check .` — clean (103 files); `ruff check .` — clean (0 findings)
- `pyright` (strict mode) — 0 errors, 0 warnings
- `lint-imports` — 2 architecture contracts kept, 0 broken
- deterministic CLI demo — `status=succeeded` with the correlated telemetry narrative and
  `[redacted]` visible; `compileall` — clean

Acceptance-gate evidence, criterion by criterion:

- *hostile test workloads cannot escape the configured workspace boundary* —
  `test_live_workload_cannot_see_host_filesystem` (host secret path unreadable, content absent
  from workload output) and `test_live_workload_cannot_write_outside_workspace` (rootfs write
  blocked, workspace write succeeds and lands in the host workspace) against the real runtime.
- *workloads requiring network isolation cannot bind to adapters that lack it* —
  `test_network_requiring_workload_binds_to_container_but_not_local` and
  `test_kernel_isolation_requirement_fails_closed_against_container` (fail-closed construction
  via `SandboxCommandTools`), plus `test_telemetry_wrapper_preserves_container_capabilities`.
- *process timeout/resource-limit behavior is demonstrated externally, not simulated* —
  `test_live_timeout_kills_container_and_children` (wall timeout kills two `sleep 300`
  children), `test_live_cpu_limit_kills_busy_workload` (hard CPU ulimit),
  `test_live_pids_limit_bounds_forking` (fork storm aborted by pids cgroup),
  `test_live_memory_limit_kills_hog` (OOM kill under 32 MiB cgroup),
  `test_live_tmpfs_is_size_bounded`, `test_live_output_is_bounded`.
- *sandbox destruction leaves no running child workload* —
  `test_live_destroy_leaves_no_running_workload` (mid-flight `destroy()` on `sleep 300`;
  `docker ps -a` confirms no `loopforge-sandbox-*` container survives) and the timeout test's
  post-kill container sweep.
- *security test suite covers traversal, symlinks, environment leakage, process/resource abuse,
  and capability mismatch* — `tests/security/test_container_sandbox.py`: traversal/absolute-path
  rejection, symlink escape (read + write, outside file untouched), byte limits,
  `test_live_host_environment_is_not_inherited`, the resource-abuse live tests above, and the
  capability-mismatch negotiation tests, alongside the unchanged local-adapter suite.

Post-cycle hardening pass (operator-initiated, 2026-08-27, after the SUCCESS classification):
four defects in the cycle's own work were found by adversarial review, fixed, and pinned —

1. a missing/unexecutable Docker binary leaked raw `OSError` from `subprocess.Popen` instead of
   the port-contract `SandboxError` (`container runtime failed to start`);
2. a wall timeout firing while the container was still starting missed the single-shot
   `docker kill` ("No such container") and let the workload run unsupervised — the kill is now
   retried on a bounded deadline until it succeeds, the CLI exits, or the deadline passes;
3. a comma in the resolved workspace root silently corrupted `--mount` bind CSV parsing — such
   roots are now rejected at construction;
4. NaN/Infinity bypassed `<= 0` validation for `CommandSpec`/`SandboxLimits`/
   `ContainerSandboxConfig` fields and both adapters' runtime timeouts (Infinity would have
   silently disabled wall-clock enforcement; NaN produced undefined wait behavior) — all are
   rejected as non-finite, and the local adapter now validates the runtime timeout before
   spawning rather than after.

Evidence: 1091 passing / 9 skipped (live daemon), 1078 passing / 22 skipped (daemon-free),
97.86% branch coverage, `adapters/container_sandbox.py` at 100%; ruff/pyright/lint-imports
unchanged green. Pins live in `tests/regression/test_hardening_regressions.py` (PACS-009
section) and `tests/security/test_container_sandbox.py` (kill-retry, deadline, truncation,
missing-binary, symlinked-root, finite-timeout tests).

## Stop

**SUCCESS.** All five acceptance-gate criteria are demonstrated by executable evidence —
including live, externally enforced isolation, timeout, resource-limit, and cleanup behavior —
and every quality gate is green with hermetic daemon-free CI preserved. Capability honesty holds
in both directions: the container adapter's stronger claims are backed by enforcement, and
`kernel_isolated=False` keeps VM/kernel-grade isolation an unsatisfiable requirement rather than
an implied one. `ConstrainedLocalSandbox` remains the development/reference adapter with its
honest capability report unchanged. Checkpoint not yet committed (awaiting operator confirmation).

## Follow-on implications

- PACS-010 can execute untrusted fixture repositories through `ContainerSandbox` by declaring
  `SandboxRequirements(process_filesystem_isolated=True, network_isolated=True)` on repair tool
  bindings; binding fails closed anywhere the container backend is unavailable.
- Wrapping the container adapter in `TelemetrySandbox` joins sandbox-execution spans to the run
  trace with capability delegation intact (pinned by test).
- A future egress allowlist/proxy `ContainerNetworkPolicy` member is the designed extension seam;
  it must stay code-owned and re-justify `network_isolated` truthfulness.
- A future VM-grade backend (e.g. microVM) could truthfully set `kernel_isolated=True` through the
  same port; workloads may already require it and fail closed today.
