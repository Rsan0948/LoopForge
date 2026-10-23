"""Resolve static evidence links only inside a pinned text snapshot."""

import ast
import posixpath
import re
from pathlib import PurePosixPath


def mask_js_comments(text: str) -> str:  # noqa: PLR0912 - bounded lexical states
    """Keep import positions while excluding comments and template text."""
    output = list(text)
    index = 0
    state = "code"
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == "/" and following == "/":
                state = "line"
            elif char == "/" and following == "*":
                state = "block"
            elif char in {"'", '"', "`"}:
                state = char
        elif state == "line":
            if char == "\n":
                state = "code"
            else:
                output[index] = " "
        elif state == "block":
            if char != "\n":
                output[index] = " "
            if char == "*" and following == "/":
                output[index + 1] = " "
                index += 1
                state = "code"
        elif char == "\\":
            index += 1
            if index < len(text) and text[index] != "\n" and state == "`":
                output[index] = " "
        elif char == state:
            if state == "`":
                output[index] = " "
            state = "code"
        elif state == "`" and char != "\n":
            output[index] = " "
        index += 1
    return "".join(output)


def resolve_module(files: dict[str, str], base: str) -> list[str]:
    return [
        p
        for p in (
            base,
            base + ".py",
            base + "/__init__.py",
            base + ".ts",
            base + ".tsx",
            base + "/index.ts",
            base + "/index.tsx",
        )
        if p in files
    ]


def imported_symbols(names: str) -> list[str]:
    """Use source export names, never alias or import grammar keywords."""
    named_symbol = re.compile(
        r"\s*(?:type\s+)?([A-Za-z_][A-Za-z_0-9]*)"
        r"(?:\s+as\s+[A-Za-z_][A-Za-z_0-9]*)?\s*"
    )
    symbols = [
        match.group(1)
        for group in re.findall(r"\{([^}]*)\}", names)
        for item in group.split(",")
        if (match := named_symbol.fullmatch(item))
    ]
    outside = re.sub(r"\{[^}]*\}", "", names).strip().rstrip(",").strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", outside):
        symbols.insert(0, outside)
    return symbols


def import_edges(  # noqa: PLR0912 - explicit static import grammar arms
    files: dict[str, str], path: str, text: str | None = None
) -> dict[str, list[str]]:
    """Parse import declarations; text mentions and shared basenames are not edges."""
    text = files[path] if text is None else text
    result: dict[str, list[str]] = {}
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return result
        parent = PurePosixPath(path).parent.parts
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                prefix = list(parent[: len(parent) - node.level + 1]) if node.level else []
                parts = prefix + (node.module.split(".") if node.module else [])
                base = "/".join(parts)
                names = [a.name for a in node.names]
                for peer in resolve_module(files, base):
                    result[peer] = names
                for name in names:
                    for peer in resolve_module(files, base + "/" + name):
                        result[peer] = names
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for peer in resolve_module(files, alias.name.replace(".", "/")):
                        result[peer] = [alias.asname or alias.name]
        return result
    text = mask_js_comments(text)
    imports = re.findall(
        r"^[ \t]*import\s+([^;]*?)\s+from\s*[\"']([^\"']+)[\"']", text, re.MULTILINE
    )
    for names, module in imports:
        if module.startswith("."):
            base = posixpath.normpath(str(PurePosixPath(path).parent / module))
        elif module.startswith("@/") and path.startswith("apps/"):
            base = "/".join(path.split("/")[:2]) + "/" + module[2:]
        else:
            continue
        for peer in resolve_module(files, base):
            result[peer] = imported_symbols(names)
    return result


def is_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return ".test." in name or name.startswith("test_") or name.endswith("_test.py")


def substantive_source(path: str, text: str) -> bool:
    if not path.endswith(".py"):
        return bool(text.strip())
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return bool(text.strip())  # Invalid source itself deserves inspection.
    return any(
        not isinstance(node, (ast.Expr, ast.Import, ast.ImportFrom, ast.Pass)) for node in tree.body
    )


def test_interval(text: str) -> tuple[int, int] | None:
    lines = text.splitlines()
    definitions = [
        i
        for i, line in enumerate(lines, 1)
        if re.search(r"\b(?:async\s+)?def\s+test_|\b(?:it|test)\s*\(", line)
    ]
    if not definitions:
        return None
    at = next(
        (
            i
            for i in definitions
            if re.search(r"reject|deni|fail|error|unauthor|forbid", lines[i - 1], re.IGNORECASE)
        ),
        definitions[0],
    )
    return max(1, at - 2), min(len(lines), at + 15)


def matching_tests(files: dict[str, str], path: str) -> list[str]:
    """Prefer exact sibling tests, otherwise require a resolved import of this source."""
    source = PurePosixPath(path)
    stem = source.stem
    exact = {
        str(source.with_name(name))
        for name in (
            stem + ".test.ts",
            stem + ".test.tsx",
            "test_" + stem + ".py",
            stem + "_test.py",
        )
    }
    candidates = [p for p in files if p != path and is_test(p) and test_interval(files[p])]
    siblings = sorted(p for p in candidates if p in exact)
    return siblings or sorted(p for p in candidates if path in import_edges(files, p))


def source_anchor(path: str, text: str) -> int:
    """Prefer executable Python branches over descriptions mentioning failure words."""
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return 1
        functions = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        branches = [
            n.lineno
            for f in functions
            for n in ast.walk(f)
            if isinstance(n, (ast.If, ast.Raise, ast.Return, ast.Try))
        ]
        if branches:
            return min(branches)
        if functions:
            return min(n.lineno for n in functions)
        statements = [
            node.lineno
            for node in tree.body
            if not isinstance(node, (ast.Expr, ast.Import, ast.ImportFrom, ast.Pass))
        ]
        if statements:
            return min(statements)
    return next(
        (
            i
            for i, line in enumerate(text.splitlines(), 1)
            if re.search(r"raise |throw |if \(!|except |return.*(?:50[023]|40[013])", line)
        ),
        1,
    )


def definition_anchor(path: str, text: str, symbols: list[str]) -> int:
    """Locate a real imported definition, never a Python comment/docstring match."""
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return 0
        return next(
            (
                node.lineno
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name in symbols
            ),
            0,
        )
    return next(
        (
            i
            for i, line in enumerate(mask_js_comments(text).splitlines(), 1)
            if any(
                re.search(r"(?:function|class|const)\s+" + re.escape(name) + r"\b", line)
                for name in symbols
            )
        ),
        0,
    )


def import_anchor(files: dict[str, str], path: str, peer: str) -> int:
    """Locate the actual pinned import establishing a reverse consumer edge."""
    text = files[path]
    if path.endswith(".py"):
        try:
            nodes = ast.walk(ast.parse(text))
        except SyntaxError:
            return 1
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                segment = ast.get_source_segment(text, node) or ""
                if peer in import_edges(files, path, segment):
                    return node.lineno
    else:
        visible = mask_js_comments(text)
        for match in re.finditer(
            r"^[ \t]*import\s+[^;]*?\s+from\s*[\"'][^\"']+[\"']", visible, re.MULTILINE
        ):
            if peer in import_edges(files, path, match.group()):
                return visible.count("\n", 0, match.start()) + 1
    return 1


def review_interval(path: str, text: str, offset: int) -> tuple[int, int]:
    """Rotate document sections; keep code/test selection source-based and bounded."""
    lines = text.splitlines()
    if path.endswith(".md"):
        headings = [i for i, line in enumerate(lines, 1) if re.match(r"^#{1,6}\s+\S", line)]
        if headings:
            index = offset % len(headings)
            start = headings[index]
            boundary = headings[index + 1] - 1 if index + 1 < len(headings) else len(lines)
            return start, min(boundary, start + 15)
    if is_test(path):
        case = test_interval(text)
        if case:
            return case
    anchor = source_anchor(path, text)
    return max(1, anchor - 3), min(len(lines), anchor + 12)
