"""Code-owned presentation of context items inside a native tool conversation.

Two problems with presenting items as plain ``- (trust) content`` lines:

* Multi-line repository content (a source file, test output) can contain a line such as
  ``- (runtime_policy) ...`` that is indistinguishable from a real runtime-owned item.
  Delimited presentation encloses multi-line content between tags derived from the
  content's own SHA-256, which the content cannot contain (that would require a
  fixed point of the hash).
* Upstream sanitizers escape newlines in command output as ``\\x0a``; dozens per result
  tokenize poorly and are hard to read. Inside a delimited block the escape is safe to
  undo, because no line in the block can be mistaken for an item.

Repeated verification failures are also shortened: when the runtime's own fingerprint
of a failure (checks, failing tests, assertion lines) is unchanged, only the fingerprint
is restated. The fingerprint is computed here from runtime-recorded output, never from
model text.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

from loopforge.domain.context import ContextItem

_FINGERPRINT_BUDGET: Final = 600
_CHECK = re.compile(r"command:([\w.-]+): (passed|failed)")
_FAILURE_LINES = re.compile(
    r"^(?:FAILED |ERROR |E {2,}|_{3,} [\w.\[\]:-]+ _{3,}$|[\w./-]+\.py:\d+: \w+)"
)
_DURATION = re.compile(r"\b(?:in|took)\s+\d+(?:\.\d+)?\s*s(?:econds?)?\b")


def _escape_controls(text: str) -> str:
    """Keep newlines and tabs; escape every other non-printable character."""
    return "".join(
        char if char.isprintable() or char in "\n\t" else f"\\x{ord(char):02x}" for char in text
    )


def readable(content: str) -> str:
    """Undo upstream newline/tab escapes, then re-escape other control characters."""
    return _escape_controls(content.replace("\\x0a", "\n").replace("\\x09", "\t"))


def delimited(item: ContextItem) -> str:
    """One labeled line for single-line content; a tagged block otherwise."""
    text = readable(item.content)
    label = item.trust.value
    if "\n" not in text:
        return f"- ({label}) {text}"
    tag = hashlib.sha256(text.encode()).hexdigest()[:12]
    return f"- ({label}) <<<{label} {tag}\n{text}\n{label} {tag}>>>"


def failure_fingerprint(content: str) -> str:
    """Stable identity of a check failure: check states, failing tests, assertions.

    Progress dots, durations, warnings and receipts are excluded, so reruns of the same
    failure on a different patch produce the same fingerprint.
    """
    text = readable(content).split("Check receipts:", 1)[0]
    checks = [f"{name}: {state}" for name, state in _CHECK.findall(text)]
    lines = [
        _DURATION.sub("", line.strip().strip("_").strip()).rstrip()
        for line in text.splitlines()
        if _FAILURE_LINES.match(line.strip()) and "Warning" not in line
    ]
    selected = list(dict.fromkeys([*checks, *lines]))
    summary = "; ".join(selected)
    if len(summary) > _FINGERPRINT_BUDGET:
        summary = summary[: _FINGERPRINT_BUDGET - 3] + "..."
    return summary


def is_verification_failure(item: ContextItem) -> bool:
    return str(item.item_id).endswith(":verifier-failure")
