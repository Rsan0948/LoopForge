# Build status — PACS-005 security + sandbox contract complete

PACS-005 establishes LoopForge's explicit security/trust vocabulary, provider-independent sandbox
contract, capability negotiation, and first constrained local execution adapter. No subsequent PACS
cycle is active until manually initiated.

## Implemented through PACS-005

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

## Verified in this build environment

- `PYTHONPATH=src python -m pytest -q` — 125 passing
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

`ruff`, `pyright`, and `lint-imports` remain configured in the repository/CI but unavailable in this
execution environment.
