# Security

LoopForge executes model-proposed actions and therefore treats model output, repository-controlled
content, tool output, and external evidence as potentially hostile. Runtime authority comes from
code-owned policy and explicit human authorization, never from instruction-like content.

## Current controls

The repository includes:

- code-owned tool risk/permission/retry/idempotency/approval/timeout metadata
- explicit trust classes for runtime policy, human authority, observations, evidence, model
  inference, and untrusted content
- a provider-independent `SandboxPort` with machine-readable capability negotiation
- a constrained local reference adapter with relative-path confinement and symlink defense for its
  file API
- fixed allowlisted argv execution with `shell=False`
- explicit child-process environment rather than ambient host-environment inheritance
- wall-clock timeout with process-group termination
- POSIX CPU/address-space/open-file/file-size limits and bounded captured output
- tool-level sandbox capability requirements that fail closed during binding
- a hardened Docker container adapter with read-only root filesystem, explicit mounts,
  dropped capabilities, bounded resources, and network isolation

## Important boundary

`ConstrainedLocalSandbox` is **not** a hostile-code isolation boundary. It truthfully reports:

- `process_filesystem_isolated=False`
- `network_isolated=False`
- `kernel_isolated=False`

Therefore arbitrary untrusted repository code must not be treated as safely executable through this
adapter. LoopForge's container adapter is the supported path for untrusted repositories; it declares
and negotiates process-filesystem, network, and kernel isolation separately, and fails closed when a
workload's required capability is unavailable. This is defense in depth, not a claim that containers
are equivalent to a dedicated VM or hostile multi-tenant boundary.

See `docs/security/threat-model.md` and `docs/architecture/sandbox-contract.md` for the full threat
model and capability contract.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue:
use GitHub's [private vulnerability reporting](https://github.com/Rsan0948/LoopForge/security/advisories/new)
("Report a vulnerability" on the repository's Security tab). Include the
affected version, reproduction steps, and which trust boundary you believe is
crossed. You can expect an acknowledgement within a few days.
