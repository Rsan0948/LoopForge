"""One-use check receipts for explicitly deterministic, immutable workloads.

No receipt survives a verifier instance. Full workspace bytes/modes and a
code-owned execution identity must match; failed checks are always rerun.
"""

import hashlib
import json
import math
import time
from pathlib import Path

from loopforge.domain.security import SandboxCapabilities
from loopforge.ports.sandbox import SandboxCommandResult, SandboxPolicyError, SandboxPort


class CheckReceiptSandbox:
    def __init__(
        self,
        sandbox: SandboxPort,
        root: Path,
        identity: dict[str, object],
        reusable: frozenset[str],
        timeouts: dict[str, float] | None = None,
    ) -> None:
        self.sandbox, self.root = sandbox, root
        self.identity = json.dumps(identity, sort_keys=True)
        self.reusable = reusable
        self.receipts: dict[str, tuple[str, SandboxCommandResult]] = {}
        self.reused = 0
        self.timeouts = dict(timeouts or {})
        self.history: list[dict[str, object]] = []

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self.sandbox.capabilities

    def read_text(self, relative_path: str) -> str:
        return self.sandbox.read_text(relative_path)

    def write_text(self, relative_path: str, content: str) -> None:
        self.sandbox.write_text(relative_path, content)

    def workspace_identity(self) -> str | None:
        entries: list[tuple[str, int, str]] = []
        total = 0
        try:
            for path in sorted(self.root.rglob("*")):
                relative = path.relative_to(self.root)
                if ".git" in relative.parts:
                    continue
                if path.is_symlink():
                    return None
                if path.is_dir():
                    continue
                total += path.stat().st_size
                if total > 64 * 1024**2 or len(entries) >= 20000:
                    return None
                entries.append(
                    (
                        str(relative),
                        path.stat().st_mode,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                )
        except OSError:
            return None
        return hashlib.sha256(json.dumps([self.identity, entries]).encode()).hexdigest()

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            message = "Check timeout must be finite and positive"
            raise SandboxPolicyError(message)
        started = time.monotonic()
        identity = self.workspace_identity() if command_name in self.reusable else None
        effective = self.timeouts.get(command_name, timeout_seconds)
        if timeout_seconds is not None and effective is not None:
            effective = min(effective, timeout_seconds)
        key = json.dumps([command_name, effective])
        prior = self.receipts.pop(key, None)
        if identity is not None and prior is not None and prior[0] == identity:
            self.reused += 1
            self.history.append(
                {
                    "command": command_name,
                    "workspace": identity,
                    "reused": True,
                    "passed": True,
                    "seconds": time.monotonic() - started,
                }
            )
            self.history = self.history[-256:]
            return prior[1]
        result = self.sandbox.run(command_name, timeout_seconds=timeout_seconds)
        self.history.append(
            {
                "command": command_name,
                "workspace": identity,
                "reused": False,
                "passed": result.succeeded,
                "seconds": time.monotonic() - started,
            }
        )
        self.history = self.history[-256:]
        if identity is not None and result.succeeded and self.workspace_identity() == identity:
            self.receipts[key] = (identity, result)
        return result
