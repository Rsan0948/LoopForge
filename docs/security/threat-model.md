# LoopForge threat model

## Security objective

LoopForge assumes that model output and repository-controlled content can be wrong, adversarial, or
actively malicious. Neither may expand runtime authority. Security-critical behavior belongs to the
control plane and execution adapter, not to prompt compliance.

## Trust classes

LoopForge defines the following origin/authority classes. They are vocabulary for later
provenance-aware context work; PACS-005 establishes the taxonomy before context implementation.

1. `RUNTIME_POLICY` — immutable host/control-plane policy; highest authority.
2. `AUTHORIZED_HUMAN` — explicit task/approval authority from the operator.
3. `DETERMINISTIC_OBSERVATION` — measured environment output such as test exit status.
4. `EXTERNAL_EVIDENCE` — retrieved evidence whose content is data, not runtime authority.
5. `MODEL_INFERENCE` — plans, hypotheses, reflections, evaluations.
6. `UNTRUSTED_CONTENT` — repository files, web text, issue text from untrusted sources, generated
   artifacts, or other content that may contain instruction-like text.

The ordering is not a generic truth score. It is an **authority distinction**. For example, a model
inference may be useful but cannot override a measured verifier result; repository text can contain
instructions but cannot grant a production-write permission.

## Protected assets

- host filesystem outside the assigned workspace
- credentials, environment variables, tokens, SSH material, cloud metadata, and secrets
- external services and customer/user data
- authoritative event history and policy configuration
- operator identity/approval decisions
- host compute, memory, process table, disk, and network
- integrity of Git/workspace artifacts used for verification

## Threat actors / hostile inputs

- a model proposing unsafe or malformed actions
- prompt injection embedded in repository/document/web content
- a malicious repository with symlinks, scripts, build hooks, or dependency behavior
- malformed provider/model/tool responses
- a compromised dependency/tool binary
- accidental operator misconfiguration
- process crashes and ambiguous side-effect outcomes

## Primary threat scenarios

### Authority escalation

A repository says "ignore policy and deploy" or a model requests an undeclared capability.

**Control:** tool metadata and permissions are code-owned. Content cannot mutate tool authority.

### Path traversal / symlink escape

A model requests `../../secret` or a repository contains a symlink from the workspace into the host.

**Control:** safe file APIs accept relative paths only, reject `..`, reject symlink components, and
verify resolved paths stay under the configured root.

### Ambient secret leakage

A child process inherits the developer shell environment and prints API keys.

**Control:** sandbox commands receive an explicit environment mapping rather than inheriting
`os.environ`.

### Shell/argument injection

Untrusted text becomes a shell command.

**Control:** the reference adapter executes only code-owned allowlisted `argv` tuples, requires an
absolute executable path, and always uses `shell=False`. PACS-005 does not support arbitrary model
shell strings.

### Resource exhaustion

Executed code loops forever or emits unbounded output.

**Control:** wall-clock timeout with process-group kill, POSIX CPU/address-space/open-file/file-size
limits, and bounded captured output.

### Network exfiltration

Executed repository code attempts to send host/repository information over the network.

**Control status:** **NOT mitigated by `ConstrainedLocalSandbox`** (`network_isolated=False`).
**Mitigated by `ContainerSandbox`** (PACS-009): the only supported network policy is deny-all
(`--network none`), so workload containers have no external interfaces; live capability-gated tests
demonstrate that egress attempts fail. Hostile/untrusted executable code must require an adapter
reporting `network_isolated=True` through code-owned `SandboxRequirements`.

### Host filesystem access from child code

A fixed allowlisted command executes repository code that opens `/etc/...` or another host path.

**Control status:** **NOT strongly mitigated by `ConstrainedLocalSandbox`**
(`process_filesystem_isolated=False`, `kernel_isolated=False`). **Mitigated by `ContainerSandbox`:**
workloads run in a mount namespace where only the assigned workspace is bind-mounted, the root
filesystem is read-only, capabilities are dropped, and `no-new-privileges` is set; live tests
demonstrate host paths are invisible and writes outside the workspace fail. This is namespace-grade,
not VM-grade, isolation: `kernel_isolated` remains `False` because containers share a kernel with
the runtime host.

### Timeout after remote success

An external side effect succeeds but the result is lost and the runtime retries.

**Control:** PACS-003 action journal + idempotency semantics; PACS-004 tests this adversarially.

## Sandbox capability rule

A sandbox adapter must truthfully advertise enforceable properties. A caller may require properties
before executing a workload. Unsupported properties fail closed rather than being inferred from an
adapter name.

For PACS-005 `ConstrainedLocalSandbox`:

| Capability | Enforced |
|---|---|
| file API confined to workspace | yes |
| symlink defense for file API | yes |
| explicit environment only | yes |
| wall-clock process timeout | yes |
| POSIX resource limits | yes (on this reference adapter/platform) |
| captured output bounded | yes |
| child process filesystem isolation | **no** |
| network isolation | **no** |
| kernel/container isolation | **no** |

Therefore the local adapter is suitable for trusted local development commands and contract testing,
**not for hostile arbitrary repository execution**.

For PACS-009 `ContainerSandbox`:

| Capability | Enforced |
|---|---|
| file API confined to workspace | yes (via composed local adapter) |
| symlink defense for file API | yes (via composed local adapter) |
| explicit environment only | yes (host env never inherited; image ENV entries still apply) |
| wall-clock process timeout | yes (named-container kill destroys the PID namespace) |
| resource limits | yes (memory/swap, pids, CPU seconds, open files, bounded tmpfs/output) |
| captured output bounded | yes |
| child process filesystem isolation | **yes** (mount namespace; only workspace bind-mounted) |
| network isolation | **yes** (deny-all `ContainerNetworkPolicy.NONE`) |
| kernel/container isolation | **no** (shared-kernel containers; no VM-grade claim) |

The container adapter is the approved boundary for untrusted repository/build execution within
those honest limits; workloads needing kernel-grade isolation cannot bind to it.

## Residual/known risks

- Local path checks are userspace checks and cannot provide the race/namespace guarantees of a
  kernel-isolated filesystem boundary.
- POSIX `resource` limits are platform-specific and are not a portable container security model.
- An allowlisted executable may itself be compromised.
- Container image trust is operator-owned: a malicious or compromised image runs inside the
  hardened container contract but its content is not vetted by LoopForge.
- Containers share a kernel with the runtime host; a container-escape vulnerability in the
  container runtime or kernel is outside this adapter's control. `kernel_isolated=False` keeps
  this explicit.
- The Docker daemon API is a privileged control point; daemon configuration and socket access
  are operator concerns, and the Docker executable path is code/config-owned (rule 14).
- Only deny-all networking exists; a future egress allowlist/proxy policy must remain code-owned
  and must not weaken the fail-closed negotiation rule.

## Security acceptance philosophy

LoopForge does not call a control "sandboxed" merely because a process has a working directory or a
timeout. Security properties are explicit capabilities, and workloads can demand capabilities the
selected adapter must prove it provides.
