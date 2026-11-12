# Independent local checkouts without duplicate storage

`loopforge.adapters.git_checkout.local_checkout(source, revision, target, branch)`
materializes a pinned local repository into a new directory. It publishes the directory
only after its files, branch, index and object store are ready.

Every checkout has independent files, objects and Git metadata. There are no hardlinks,
remotes, alternate object stores or linked-worktree host pointers. Original commit objects
remain available, so a consumer can compare a later candidate against an earlier baseline.
Deleting the source repository does not break the checkout.

The file-copy adapter uses APFS clones on macOS and FICLONE on supported Linux filesystems.
Those operations share physical blocks while retaining distinct inodes: modifying either
file does not modify the other. Unsupported cloning falls back to an ordinary independent
copy. Disk-full and permission failures are not disguised as unsupported cloning.

Working-tree files are reused only if their copied bytes match the selected Git blob.
Dirty, missing or unreadable source files are materialized from the pinned object instead.
Source symlinks are never followed to copy external files. Symlinks actually recorded in
the pinned tree are materialized by Git. Submodules remain deinitialized; nothing fetches
remote data. The input must have a complete local object store; partial/promisor and
alternate-backed sources are refused before publication. Source linked worktrees can be
used, but the resulting checkout is self-contained.

`file_copy.copy_file` creates a new independent regular file and retains mode/timestamps.
Its source and target parents must be canonical, existing directories without symlinks.
`require_clone=True` forbids a full-copy fallback, useful when compacting existing duplicate
payloads. `copy_metadata` preserves stat metadata, ACLs and extended attributes independently
of file data. Both functions traverse directories using no-follow descriptors.

This is a storage optimization, not a new sandbox capability. A local working directory
is not strong isolation. Execution must still satisfy the selected sandbox's code-owned
capability requirements. Copy-on-write sharing also does not remove Git or file metadata
costs, logs, SQLite journals, or the need for an archive retention policy. Tools such as
`du` can count shared blocks repeatedly; physical free space should be observed separately.
