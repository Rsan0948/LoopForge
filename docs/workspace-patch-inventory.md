# Workspace patch inventories and native JSON-text arguments

`WorkspacePort.patch_status()` describes the net patch relative to `base_revision`,
including committed changes and current untracked/included ignored files. It uses
`WorkspaceStatus` as its path inventory. `status()` still describes the working tree
relative to HEAD: a committed checkpoint can be clean while having a nonempty patch.
A change reverted to its original baseline bytes is not a patch, even when HEAD differs.

Custom workspace adapters must implement the new port operation explicitly. Repair
patch constraints consume this inventory; adapters must report additions, deletions,
mode changes and renamed paths as delete/add consistently with their baseline diff.
Do not silently substitute HEAD-relative status. The domain remains independent of Git.

`ModelToolSpec.json_text_parameters` optionally declares a `JsonTextParameter` for a
specific parameter whose runtime representation is JSON text. Its code-owned schema
is presented to supported native providers, and its encoder validates structured input
before converting it into the existing string-valued `ActionProposal.arguments`.
OpenRouter and Ollama apply this conversion and preserve structured assistant-call
history. Specs without declared encodings keep their original provider schemas and
strict string arguments. Legacy/text callers keep the string representation.

Encoders must reject unsupported structures and enforce their workload's fixed limits.
No undeclared object/array coercion occurs. This catalog does not supply permissions,
risk, approval, retry or sandbox metadata: the registered tool contract still authorizes
every proposed action, and the executor still validates hashes and edit semantics.
