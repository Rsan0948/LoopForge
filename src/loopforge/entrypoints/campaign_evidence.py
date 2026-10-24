"""Code-owned campaign snapshot and observed-interval contracts."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from loopforge.adapters.git_workspace import GitWorkspace

MAX_FILE_BYTES = 500_000
MAX_SNAPSHOT_BYTES = 30_000_000
MAX_SNAPSHOT_FILES = 5000


def source_allowed(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not any(part.startswith(".env") or part in {".git", ".ssh"} for part in path.parts)
        and path.name not in {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
        and path.suffix.lower() not in {".pem", ".key", ".p12", ".pfx", ".der"}
    )


def candidate_snapshot(workspace: GitWorkspace) -> dict[str, str]:  # noqa: PLR0912, PLR0915 - explicit fail-closed source classifications
    changed = set(workspace.status().files)
    paths = set(workspace.run_git("ls-files", "-z").split("\0")) | changed
    files: dict[str, str] = {}
    total = 0
    for name in sorted(paths - {""}):
        if not source_allowed(name):
            if name in changed:
                msg = f"candidate changes excluded source: {name}"
                raise ValueError(msg)
            continue
        path = workspace.root / name
        if not path.resolve().is_relative_to(workspace.root.resolve()) or path.is_symlink():
            if name in changed:
                msg = f"candidate changes unsupported symlink: {name}"
                raise ValueError(msg)
            continue
        if path.is_file():
            raw = path.read_bytes()
        elif name in changed:
            # Deleted source is reviewed on the baseline side of its deletion hunk.
            raw = workspace.run_git("show", f"{workspace.base_revision}:{name}").encode()
        else:
            continue
        if len(raw) > MAX_FILE_BYTES:
            if name in changed:
                msg = f"mandatory candidate source exceeds bounds: {name}"
                raise ValueError(msg)
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            if name in changed:
                msg = f"mandatory candidate source is binary: {name}"
                raise ValueError(msg) from None
            continue
        if "\x00" in content:
            if name in changed:
                msg = f"mandatory candidate source is binary: {name}"
                raise ValueError(msg)
            continue
        total += len(raw)
        if total > MAX_SNAPSHOT_BYTES or len(files) >= MAX_SNAPSHOT_FILES:
            msg = "candidate snapshot exceeds bounds"
            raise ValueError(msg)
        files[name] = content
    diff = workspace.diff()
    # Filtered paths cannot reach the model through the separate diff channel.
    if any(not source_allowed(name) for name in changed):
        msg = "candidate diff contains excluded source"
        raise ValueError(msg)
    if len(diff.encode()) + total > MAX_SNAPSHOT_BYTES:
        msg = "candidate diff exceeds snapshot bounds"
        raise ValueError(msg)
    files["CAMPAIGN_PATCH.diff"] = diff
    return files


def review_obligations(
    files: dict[str, str], expected: set[str]
) -> dict[str, list[tuple[int, int]]]:
    obligations: dict[str, list[tuple[int, int]]] = {}
    diff = files.get("CAMPAIGN_PATCH.diff", "")
    path = ""
    old_path = ""
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            old_path = line[6:]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line == "+++ /dev/null":
            path = old_path
        elif line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match and path in expected:
                _old_start, _old_count, new_start, new_count = match.groups()
                deleted = int(new_count or "1") == 0
                if deleted:
                    # Deleted lines live only in CAMPAIGN_PATCH.diff. The
                    # candidate can show the nearest surviving context, not
                    # an old line number beyond its new end of file.
                    candidate_lines = len(files.get(path, "").splitlines())
                    if candidate_lines:
                        at = min(max(1, int(new_start)), candidate_lines)
                        obligations.setdefault(path, []).append((at, at))
                    continue
                start = int(new_start)
                count = int(new_count or "1")
                if count:
                    obligations.setdefault(path, []).append((max(1, start), start + count - 1))
    for name in expected:
        if name not in files:
            msg = f"mandatory review input unavailable: {name}"
            raise ValueError(msg)
        if name == "CAMPAIGN_PATCH.diff" or name not in obligations:
            obligations[name] = [(1, len(files[name].splitlines()))]
    return obligations


def intervals_cover(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    if end < start:
        return True  # Empty-file metadata has no invented source lines.
    cursor = start
    for left, right in sorted(spans):
        if left > cursor:
            break
        if right >= cursor:
            cursor = right + 1
        if cursor > end:
            return True
    return False


def missing_coverage(
    excerpts: dict[tuple[str, int], str],
    obligations: dict[str, list[tuple[int, int]]],
    *,
    files: dict[str, str] | None = None,
) -> list[str]:
    """Check literal observed line spans against the pinned source metadata.

    Joined source pages retain trailing blank lines. A pinned nonempty file
    distinguishes a blank-line excerpt from an empty-file observation. Without
    that metadata retain the conservative legacy interpretation of empty text.
    """
    missing: list[str] = []
    for path, ranges in obligations.items():
        spans: list[tuple[int, int]] = []
        for (name, at), text in excerpts.items():
            if name != path:
                continue
            if files is None:
                count = len(text.splitlines())
            elif files.get(name):
                count = len(text.split("\n"))
            else:
                count = 0
            spans.append((at, at + count - 1))
        for start, end in ranges:
            if (
                end < start
                and (
                    not any(name == path for name, _ in excerpts)
                    or (files is not None and files.get(path) != "")
                )
            ) or not intervals_cover(spans, start, end):
                missing.append(f"{path}:{start}-{end}")
    return missing


def required_reads(files: dict[str, str], obligations: dict[str, list[tuple[int, int]]]) -> int:
    """Conservative page count using the same whole-line limits as source tools."""
    reads = 0
    for path, ranges in obligations.items():
        lines = files[path].splitlines()
        covered_until = 0
        if not lines:
            reads += 1  # Explicit empty-file observation.
        for start, end in sorted(ranges):
            if start < 1 or end > len(lines) or (end < start and (lines or (start, end) != (1, 0))):
                msg = f"mandatory interval outside source: {path}:{start}-{end}"
                raise ValueError(msg)
            cursor = max(start, covered_until + 1)
            while cursor <= end:
                size, count = 0, 0
                for line in lines[cursor - 1 : cursor + 199]:
                    added = len(line) + bool(count)
                    if size + added > 3000:
                        break
                    size += added
                    count += 1
                if count == 0:
                    reads += (len(lines[cursor - 1]) + 2999) // 3000
                    cursor += 1
                    covered_until = cursor - 1
                    continue
                reads += 1
                cursor += count
                covered_until = cursor - 1
    return reads


@dataclass(frozen=True)
class SourcePage:
    text: str
    start_line: int
    start_column: int
    end_line: int
    next_line: int | None
    next_column: int
    empty: bool = False


def source_page(
    content: str, start: int = 1, column: int = 0, *, page_chars: int = 3000
) -> SourcePage:
    """Exact bounded source; a column cursor makes every long-line character reachable.

    ``page_chars`` is code-owned: review read accounting relies on the 3,000 default.
    """
    if not 3000 <= page_chars <= 16000:
        msg = "page_chars must be between 3000 and 16000"
        raise ValueError(msg)
    lines = content.splitlines()
    if not lines and start == 1 and column == 0:
        return SourcePage("", 1, 0, 0, None, 0, True)
    if start < 1 or start > len(lines) or column < 0 or column > len(lines[start - 1]):
        msg = "Source selector outside file; use returned line/column continuation"
        raise ValueError(msg)
    line = lines[start - 1]
    if column or len(line) > page_chars:
        stop = min(len(line), column + page_chars)
        more = stop < len(line)
        return SourcePage(
            line[column:stop],
            start,
            column,
            start,
            start if more else start + 1 if start < len(lines) else None,
            stop if more else 0,
        )
    selected: list[str] = []
    size = 0
    for line in lines[start - 1 : start + page_chars // 15 - 1]:
        added = len(line) + bool(selected)
        if size + added > page_chars:
            break
        selected.append(line)
        size += added
    end = start + len(selected) - 1
    return SourcePage("\n".join(selected), start, 0, end, end + 1 if end < len(lines) else None, 0)
