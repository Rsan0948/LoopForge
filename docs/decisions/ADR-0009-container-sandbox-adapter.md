# ADR-0009 — Container sandbox adapter with honest capability bounds

Status: Accepted

## Context

ADR-0008 established explicit sandbox capabilities and fail-closed negotiation, but the only
adapter (`ConstrainedLocalSandbox`) honestly advertises no process-filesystem, network, or
kernel isolation. PACS-010 (software-repair workload) will execute untrusted repository code,
which the threat model defers to a container/VM adapter with real namespace isolation.

## Decision

Add `ContainerSandbox`, a `SandboxPort` adapter that executes allowlisted `CommandSpec`s inside
hardened Docker containers driven through the Docker CLI:

- **Docker CLI, not an SDK.** The adapter shells out to a configurable `docker` executable with
  `shell=False`. This adds zero dependencies, keeps deterministic CI hermetic (no daemon is
  needed unless live isolation tests are explicitly provisioned), and makes the entire
  enforcement surface an auditable argv pinned by tests.
- **Hardened-by-construction flags.** Every workload runs with `--rm --init --pull never`
  (no implicit registry access), a unique `loopforge-sandbox-*` name, `--network none`
  (the only supported `ContainerNetworkPolicy`), `--read-only` rootfs with a size-bounded
  noexec/nosuid tmpfs at `/tmp`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--log-driver none`, memory/swap pinned equal, pids/CPU-seconds/open-file limits, and a
  single bind mount: the configured workspace at `/workspace`.
- **Composition for the file API.** `read_text`/`write_text` delegate to an internal
  `ConstrainedLocalSandbox`, so the PACS-005 traversal/symlink/byte-limit defenses are retained
  unchanged rather than reimplemented.
- **Code-owned configuration.** `ContainerSandboxConfig` is a frozen validated dataclass; image,
  command allowlist, environment, network policy, resource ceilings, and the Docker executable
  are bootstrap authority. Repository or model content can never widen them (rule 14).
- **Cleanup is structural.** Timeout kills the named container (`docker kill`), which destroys
  its PID namespace — no child workload can survive, unlike subprocess-group kills.
  `destroy()`/context-manager exit kills any in-flight container. Docker CLI failures (exit 125
  with the `docker:` stderr marker) surface as `SandboxError`, never as workload exit codes.
- **Honest capability report.** The adapter advertises `process_filesystem_isolated=True` and
  `network_isolated=True` because it enforces them, and keeps `kernel_isolated=False`:
  containers share a kernel with the runtime host, and the Docker Desktop Linux VM is an
  implementation detail of the runtime, not a guarantee this adapter enforces. Kernel-isolation
  requirements therefore still fail closed against this adapter.

## Consequences

Positive:

- untrusted repository/build workloads can run with enforced mount/network/PID isolation;
- the capability contract is truthful in both directions: network-isolation workloads can bind
  here but not to the local adapter; kernel-isolation workloads bind to neither;
- timeout/resource-limit/cleanup behavior is demonstrated by live, capability-gated tests
  against the real container runtime, while the default suite passes daemon-free;
- telemetry joins for free via `TelemetrySandbox` (capabilities delegate unchanged).

Negative:

- requires a Docker-compatible runtime for real isolation; without it the adapter cannot be
  instantiated for actual hostile execution (tests skip with reason codes);
- image trust, daemon configuration, and container-escape risk remain operator concerns,
  recorded in the threat model;
- an egress allowlist/proxy network policy is deferred; only deny-all exists.
