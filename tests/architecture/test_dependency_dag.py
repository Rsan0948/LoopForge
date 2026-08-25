from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "loopforge"

# Lower number = more foundational. A module may depend only on its own or a
# more foundational layer, except adapters which implement ports and are wired
# by entrypoints/bootstrap code rather than application/domain code.
LAYER = {
    "domain": 0,
    "ports": 1,
    "application": 2,
    "entrypoints": 3,
}


def _internal_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.name for alias in node.names if alias.name.startswith("loopforge.")
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("loopforge.")
        ):
            modules.add(node.module)
    return modules


def test_core_dependency_dag_has_no_upward_imports() -> None:
    violations: list[str] = []
    for source_layer, source_rank in LAYER.items():
        for path in (SRC / source_layer).rglob("*.py"):
            for module in _internal_imports(path):
                target_layer = module.split(".")[1]
                if target_layer in LAYER and LAYER[target_layer] > source_rank:
                    violations.append(f"{path.relative_to(SRC)} -> {module}")
                if (
                    source_layer in {"domain", "ports", "application"}
                    and target_layer == "adapters"
                ):
                    violations.append(f"{path.relative_to(SRC)} -> {module}")
    assert violations == [], "Forbidden dependency edges:\n" + "\n".join(violations)


def test_domain_has_no_external_runtime_dependencies() -> None:
    violations: list[str] = []
    for path in (SRC / "domain").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    allowed = {
                        "__future__",
                        "collections",
                        "math",
                        "dataclasses",
                        "datetime",
                        "enum",
                        "typing",
                        "loopforge",
                    }
                    if root not in allowed:
                        violations.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                allowed = {
                    "__future__",
                    "collections",
                    "math",
                    "dataclasses",
                    "datetime",
                    "enum",
                    "typing",
                    "loopforge",
                }
                if root not in allowed:
                    violations.append(f"{path.name}: from {node.module}")
    assert violations == [], "Domain external dependencies:\n" + "\n".join(violations)
