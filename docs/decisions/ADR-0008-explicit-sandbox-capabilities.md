# ADR-0008 — Sandbox security properties are explicit capabilities

Status: Accepted

## Context

A common agent-system failure is treating "runs in a working directory" or "runs in a subprocess" as
a security sandbox. That overstates what the execution layer can actually prevent and makes it easy
to execute hostile repository code with host filesystem/network access.

PACS-005 must introduce an execution abstraction before a Docker/VM implementation is available in
the current build environment.

## Decision

LoopForge defines a provider-independent `SandboxPort`, an explicit `SandboxCapabilities` value,
and code-owned `SandboxRequirements` for workloads/tools. Sandbox tool registration checks the
requirements before dispatch. Adapters/workloads fail closed when the selected sandbox cannot
satisfy the declared isolation contract.

The first adapter is `ConstrainedLocalSandbox`. It confines LoopForge's own file API, rejects path
traversal/symlinks, uses fixed code-owned commands with `shell=False`, filters the environment,
enforces wall/resource/output limits through a dedicated child launcher, and kills timed-out process groups.

It explicitly advertises:

- `process_filesystem_isolated=False`
- `network_isolated=False`
- `kernel_isolated=False`

Therefore it is not approved as a hostile-code isolation boundary.

## Consequences

Positive:
- security claims are machine-readable and testable
- future Docker/VM/remote sandboxes can share the same port
- workloads can demand stronger isolation and fail closed
- no arbitrary shell string is required for the software-repair workload

Negative:
- the local adapter remains unsuitable for truly untrusted executable repositories
- capability negotiation adds an explicit bootstrap/policy step
- strong isolation is deferred until an appropriate OS/container backend exists
