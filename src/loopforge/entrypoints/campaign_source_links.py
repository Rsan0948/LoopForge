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
