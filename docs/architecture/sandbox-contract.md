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
