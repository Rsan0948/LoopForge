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
import json
import math
import re
from typing import Final, cast

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

    The digest covers the complete normalized report. Excerpts and regex-selected
    assertion lines are presentation only; none may hide a changed failure tail.
    """
    text = readable(content)
    checks = [f"{name}: {state}" for name, state in _CHECK.findall(text)]
    lines = [
        _DURATION.sub("", line.strip().strip("_").strip()).rstrip()
        for line in text.splitlines()
        if _FAILURE_LINES.match(line.strip()) and "Warning" not in line
    ]
    facts = [line for line in text.splitlines() if line.startswith("Verification facts: ")]
    # Legacy unstructured failures still need an identity that covers the entire
    # report, including hidden requirements and arbitrary preflight reasons.
    remainder = [
        _DURATION.sub("", _receipt_metadata(line)).strip()
        for line in text.splitlines()
        if line.strip()
        and not line.startswith("Candidate unchanged since this verification")
        and "Warning" not in line
    ]
    digest = hashlib.sha256("\n".join([*facts, *remainder]).encode()).hexdigest()
    selected = list(dict.fromkeys([*checks, *lines]))
    summary = "; ".join(selected)
    if len(summary) > _FINGERPRINT_BUDGET:
        summary = summary[: _FINGERPRINT_BUDGET - 75] + "..."
    return (summary[: _FINGERPRINT_BUDGET - 72] + " sha256:" + digest).strip()


def _receipt_metadata(line: str) -> str:  # noqa: PLR0911 - strict shape guards preserve unknown content
    """Normalize a complete receipt line, never discard text following a marker."""
    prefix = "Check receipts: "
    if not line.startswith(prefix):
        return line
    try:
        parsed: object = json.loads(line[len(prefix) :])
    except ValueError:
        return line
    if not isinstance(parsed, list):
        return line
    records: list[dict[str, object]] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, dict):
            return line
        record = cast(dict[str, object], item)
        if not set(record) <= {"command", "workspace", "reused", "passed", "seconds"}:
            return line
        seconds = record.get("seconds")
        if seconds is not None and (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
        ):
            return line
        records.append(
            {
                key: value
                for key, value in record.items()
                if key not in {"seconds", "workspace", "reused"}
            }
        )
    return prefix + json.dumps(records, sort_keys=True)


def is_verification_failure(item: ContextItem) -> bool:
    return str(item.item_id).endswith(":verifier-failure")


_OMITTED = (
    "[omitted at a context checkpoint: {size} characters; use an original observation "
    "reference if provided. Re-running a tool measures current state, not this historical output.]"
)
_BLOCK_OPEN = re.compile(r"^- \((\w+)\) <<<(\w+) ([0-9a-f]{12})$")


def checkpoint_stub(content: str) -> str:
    """Restorable stub of a tool result: item labels and first lines stay, bodies go.

    Single-line items are kept (clipped). A delimited block keeps its tags, its first
    body line (for read_file: the path and sha256, so the source can be re-read) and an
    omission note, all still enclosed, so no body line ever escapes its block.
    """
    if "omitted at a context checkpoint" in content:
        return content
    lines = content.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        opened = _BLOCK_OPEN.match(line)
        if opened:
            close = f"{opened.group(2)} {opened.group(3)}>>>"
            end = next((j for j in range(index + 1, len(lines)) if lines[j] == close), None)
            if end is None:
                break
            body = lines[index + 1 : end]
            size = sum(len(b) + 1 for b in body)
            kept += [line, body[0][:300] if body else "", _OMITTED.format(size=size), close]
            index = end + 1
            continue
        if line.startswith(("- (", "Runtime update:")):
            kept.append(line[:300])
        index += 1
    return "\n".join(kept)
