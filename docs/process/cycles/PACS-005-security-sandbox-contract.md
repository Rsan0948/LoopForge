# PACS-005 — Security + sandbox contract

Status: **SUCCESS**

## Objective

Establish a security and sandbox boundary that is explicit, capability-driven, and conservative about what the local execution backend can actually isolate.

## Why now

PACS-003 established retry/idempotency semantics and PACS-004 established systematic fault injection. Before adding richer context, repository workloads, or live models, LoopForge needed a code-owned execution-security contract so future tools cannot accidentally inherit developer-machine authority.

## In scope

- explicit trust classes;
- `SandboxPort`;
- machine-readable `SandboxCapabilities`;
- code-owned `SandboxRequirements`;
- capability negotiation during tool binding;
- relative-path confinement;
- path traversal rejection;
- symlink escape rejection;
- atomic bounded file writes;
- fixed allowlisted argv execution;
- `shell=False`;
- explicit environment construction with no ambient host-environment inheritance;
- wall-clock timeout and process-group termination;
- POSIX CPU, memory, open-file, and file-size limits where supported;
- bounded captured process output;
- sandbox-backed tools integrated with the existing tool/reliability contracts;
- security documentation that distinguishes trusted local execution from hostile-code isolation.

## Out of scope

- claiming that the local subprocess adapter provides kernel isolation;
- claiming network isolation where none exists;
- arbitrary hostile-repository execution as safe;
- Docker/VM/Firecracker implementation;
- live model integration.

## Plan

1. Define sandbox capabilities and requirements in code.
2. Implement a constrained local adapter with honest capability reporting.
3. Route filesystem/process tools through the sandbox boundary.
4. Enforce tool-to-sandbox capability compatibility at binding time.
5. Add adversarial tests for traversal, symlink escape, environment leakage, timeout/resource behavior, command validation, and capability mismatch.
6. Reconcile `SECURITY.md`, `AGENTS.md`, README, product map, and build status.

## Act

The local sandbox now provides bounded developer-machine execution with:

- fixed argv and no shell interpolation;
- controlled working directory;
- explicit environment;
- path and symlink confinement for file APIs;
- timeout/process-group termination;
- supported POSIX resource limits;
- bounded output capture;
- atomic bounded writes.

The adapter reports its limits explicitly. In particular, the local adapter does **not** claim:

```text
process_filesystem_isolated = false
network_isolated            = false
kernel_isolated             = false
```

A critical hardening step was added at the tool-binding boundary: tools/workloads may declare required sandbox capabilities, and registration fails immediately if the selected adapter cannot satisfy them. For example, a workload requiring `network_isolated=true` cannot bind to the constrained local sandbox.

This converts capability reporting from documentation into an enforceable contract.

## Check

Final executable evidence recorded for the cycle:

- **125 tests passing**;
- **93% branch-aware coverage**;
- security suite **22/22 passing**;
- deterministic demo passing;
- compile checks passing;
- architecture DAG checks passing;
- diff hygiene passing;
- source/test line-length checks passing.

Ruff, Pyright, and Import Linter remained configured but were unavailable in the execution environment, so they were not claimed as executed locally.

## Stop

Classification: **SUCCESS**.

The cycle established an enforceable sandbox/security contract without overstating the protections of the local adapter.

Implementation checkpoint:

- `e4e5216 feat: add security and sandbox contract`
- final hardening: `624658d security: enforce sandbox capability requirements`

PACS-006 remained planned and was not started.

## Follow-on implications

A later hardened container/VM sandbox must implement stronger isolation capabilities before arbitrary hostile code is considered appropriately isolated. Future repository, model, and orchestration work should bind through the sandbox capability contract rather than bypassing it.
