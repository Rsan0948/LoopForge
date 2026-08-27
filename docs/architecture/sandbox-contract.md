# Sandbox contract

PACS-005 introduces `SandboxPort` as the provider-independent execution boundary for repository
workloads.

## Port surface

The initial port deliberately stays small:

- `capabilities` — enforceable security properties
- `read_text(relative_path)` — confined file read
- `write_text(relative_path, content)` — confined atomic file write
- `run(command_name)` — execute a pre-registered command capability

The model never supplies an arbitrary shell command to this port.

## Code-owned command capability

`CommandSpec` is configured by trusted application/bootstrap code and contains a fixed absolute
`argv`, wall timeout, CPU limit, and allowed exit codes. The command name is the only selector at the
sandbox interface. The runtime tool timeout may only tighten the command timeout; the adapter uses
the stricter of the two limits. POSIX rlimits are applied by a dedicated child launcher before it
`exec`s the allowlisted target, avoiding `preexec_fn` in the parent process.

This gives the later software-repair layer a path to expose explicit capabilities such as
`run_pytest`, `run_ruff`, or `run_typecheck` without exposing an unconstrained shell by default.

## Runtime bridge

`SandboxCommandTools` adapts fixed sandbox commands to the existing `ToolExecutorPort`. Tool risk,
permission, retry, idempotency, approval, and timeout still come from `ToolMetadata`. Sandbox
execution does not get to redefine those semantics. Each `SandboxToolBinding` may also carry a
code-owned `SandboxRequirements` value. Registration fails before execution if the selected sandbox
does not provide every required capability. This prevents honest capability reporting from being
accidentally ignored by a workload.

Sandbox failures map to observed tool failures:

- wall timeout → transient `SANDBOX_TIMEOUT`
- sandbox policy violation → permanent `SANDBOX_POLICY`
- other sandbox execution error → permanent `SANDBOX_EXECUTION`
- disallowed process exit → permanent `SANDBOX_EXIT_<code>`

The PACS-003 reliability policy remains authoritative about whether a retry is permitted.

## Reference adapter boundary

`ConstrainedLocalSandbox` is intentionally named to avoid implying container security. It provides a
hardened userspace contract for local trusted commands but advertises no network, child-filesystem, or
kernel isolation. A future container/VM adapter must implement the same port and advertise stronger
capabilities only when they are actually enforced.

## Container adapter boundary (PACS-009)

`ContainerSandbox` (`adapters/container_sandbox.py`) implements the same port for untrusted
repository/build workloads. Commands execute inside hardened Docker containers driven through the
Docker CLI (`shell=False`, zero new dependencies, auditable argv):

- only the configured workspace is bind-mounted (at `/workspace`); the container root filesystem is
  read-only and `/tmp` is a size-bounded `noexec,nosuid` tmpfs;
- the only supported `ContainerNetworkPolicy` is deny-all (`--network none`); egress allowlists are a
  future code-owned extension;
- `--cap-drop ALL`, `--security-opt no-new-privileges`, `--init`, `--pull never`, `--log-driver none`;
- memory/swap (pinned equal), pids, CPU-seconds, and open-file limits are enforced by the container
  runtime; captured stdout/stderr stay adapter-bounded;
- wall-clock timeout kills the named container, destroying its PID namespace so no child workload
  survives; `destroy()`/context-manager exit kills any in-flight container; Docker CLI failures
  (exit 125 with the `docker:` marker) surface as `SandboxError`, never as workload exit codes;
- the environment is explicit (`--env` pairs from the code-owned mapping); the host process
  environment is never inherited.

The file API (`read_text`/`write_text`) delegates to a composed `ConstrainedLocalSandbox`, so the
traversal/symlink/byte-limit defenses are identical to the local adapter. `ContainerSandboxConfig`
is frozen and validated at construction: image, command allowlist, environment, network policy,
resource ceilings, and the Docker executable are bootstrap authority that repository or model
content can never widen (AGENTS.md rule 14).

The adapter advertises `process_filesystem_isolated=True` and `network_isolated=True` because it
enforces them, and keeps `kernel_isolated=False`: containers share a kernel with the runtime host,
and the Docker Desktop Linux VM is an implementation detail, not an enforced contract. Workloads
requiring kernel isolation still fail closed against this adapter. Live isolation behavior is
demonstrated by capability-gated security tests that skip with reason codes when no Docker daemon
or pinned test image is available, keeping deterministic CI hermetic (ADR-0009).
