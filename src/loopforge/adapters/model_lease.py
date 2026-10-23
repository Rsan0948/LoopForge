"""Process-owned local model exclusion; a dead process releases its flock.

The lock file is never unlinked: replacing its inode would break exclusion.
This coordinates managed local clients only, not arbitrary Ollama callers.
"""

import fcntl
import json
import os
from pathlib import Path
from typing import BinaryIO


class ModelLease:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".local/state/loopforge/model.lock"
        self.handle: BinaryIO | None = None

    def acquire(self, owner: str) -> None:
        if self.handle is not None:
            msg = "model lease is already held by this client"
            raise ValueError(msg)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            msg = "another managed local model job owns the model lease"
            raise ValueError(msg) from None
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"owner": owner, "pid": os.getpid()}).encode())
        handle.flush()
        self.handle = handle

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None
