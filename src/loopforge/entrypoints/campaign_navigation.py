"""Bounded revision/content-bound source navigation, inspired by Aider's repo map.

This index is a navigation aid, not proof of behavior or a capability grant.
Python definitions use AST; other source types use conservative declaration
patterns. Literal search remains available for unresolved/unsupported syntax.
"""

import ast
import hashlib
import json
import re
from functools import lru_cache
from pathlib import PurePosixPath

from loopforge.entrypoints.campaign_evidence import source_allowed
from loopforge.entrypoints.campaign_source_links import import_edges

SOURCE_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".sql",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".sh",
    ".toml",
}
JS_DECLARATION = re.compile(
    r"^\s*(?:(?:export|default|declare|async)\s+)*(?:function|class|interface|type|const|let)\s+([A-Za-z_$][\w$]*)"
)
JS_METHOD = re.compile(r"^\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(\s*$")
JS_SUFFIXES = {".ts", ".tsx", ".js", ".jsx"}
MAX_SYMBOL_OFFSET = 1000
MAX_STORED_MATCHES = MAX_SYMBOL_OFFSET + 30


def lexical_body_end(lines: list[str], start: int) -> int:
    """Conservative same-indent body bound for navigation, never syntax proof."""
    opening = lines[start - 1]
    indent = len(opening) - len(opening.lstrip())
    closing = re.compile(r"^" + " " * indent + r"\}[,;]?\s*(?://.*)?$")
    for number in range(start + 1, len(lines) + 1):
        if closing.match(lines[number - 1]):
            return number
    return start


@lru_cache(maxsize=64)
def symbols(path: str, content: str) -> tuple[tuple[str, int, int], ...]:
    if path.endswith(".py"):
        try:
            tree = ast.parse(content)
            return tuple(
                (node.name, node.lineno, node.end_lineno or node.lineno)
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            )
        except (SyntaxError, ValueError):
            pass
    if PurePosixPath(path).suffix not in JS_SUFFIXES:
        return ()
    lines = content.splitlines()
    return tuple(
        (match.group(1), number, lexical_body_end(lines, number))
        for number, line in enumerate(lines, 1)
        if (match := JS_DECLARATION.match(line) or JS_METHOD.match(line))
    )


def repository_map(files: dict[str, str], query: str, *, max_chars: int = 6000) -> str:
    words = set(re.findall(r"[A-Za-z_][\w-]{2,}", query.casefold()))
    entries: list[tuple[int, str, dict[str, object]]] = []
    for path, content in files.items():
        if not source_allowed(path) or PurePosixPath(path).suffix not in SOURCE_SUFFIXES:
            continue
        defs = symbols(path, content)
        names = " ".join(name for name, _, _ in defs)
        score = sum(5 for word in words if word in path.casefold())
        score += sum(2 for word in words if word in names.casefold())
        entries.append(
            (
                -score,
                path,
                {
                    "path": path,
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "symbols": [
                        {"name": name, "start_line": start, "end_line": end}
                        for name, start, end in defs[:20]
                    ],
                },
            )
        )
    # Rank direct imports and callers of task-relevant files without treating
    # this conservative static graph as semantic proof.
    relevant = {path for score, path, _ in entries if score < 0}
    links = {path: set(import_edges(files, path)) for _, path, _ in entries}
    entries = [
        (
            score
            - 3 * len(links[path] & relevant)
            - 3 * sum(path in links[peer] for peer in relevant),
            path,
            {**entry, "imports": sorted(links[path])[:12]},
        )
        for score, path, entry in entries
    ]
    selected: list[dict[str, object]] = []
    for _, _, entry in sorted(entries):
        if len(json.dumps([*selected, entry])) > max_chars - 300:
            continue
        selected.append(entry)
    return json.dumps(
        {
            "trust": "untrusted_content",
            "purpose": "navigation only; read exact source",
            "files": selected,
            "omitted_files": len(entries) - len(selected),
            "resolution": "Python AST; other languages conservative syntax; aliases unresolved",
        }
    )


def find_symbol(files: dict[str, str], query: str, *, offset: int = 0) -> str:
    if not re.fullmatch(r"[A-Za-z_$][\w$]{0,119}", query):
        msg = "symbol must be one bounded identifier"
        raise ValueError(msg)
    if not 0 <= offset <= MAX_SYMBOL_OFFSET:
        msg = "symbol offset must be between 0 and 1000"
        raise ValueError(msg)
    definitions: list[dict[str, object]] = []
    references: list[dict[str, object]] = []
    definition_count = 0
    reference_count = 0
    needle = re.compile(r"(?<![\w$])" + re.escape(query) + r"(?![\w$])")
    for path, content in sorted(files.items()):
        if not source_allowed(path):
            continue
        named = {start: end for name, start, end in symbols(path, content) if name == query}
        for line, text in enumerate(content.splitlines(), 1):
            if needle.search(text):
                match: dict[str, object] = {
                    "path": path,
                    "start_line": line,
                    "end_line": named.get(line, line),
                    "kind": "definition" if line in named else "textual_reference",
                }
                if line in named:
                    definition_count += 1
                    if len(definitions) < MAX_STORED_MATCHES:
                        definitions.append(match)
                else:
                    reference_count += 1
                    if len(references) < MAX_STORED_MATCHES:
                        references.append(match)
    ranked = definitions + references
    page = ranked[offset : offset + 30]
    total = definition_count + reference_count
    more = offset + len(page) < total
    next_offset = offset + len(page) if more and offset + len(page) <= MAX_SYMBOL_OFFSET else None
    return json.dumps(
        {
            "matches": page,
            "capped": more,
            "next_offset": next_offset,
            "total_matches": total,
            "pagination_limit_reached": more and next_offset is None,
            "resolution": (
                "lexical navigation only; read exact source to verify definitions and bounds"
            ),
        }
    )
