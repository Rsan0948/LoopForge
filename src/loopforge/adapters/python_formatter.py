"""Pinned Ruff import formatting in read-only-source containers; no host write."""

import ast
import hashlib
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from loopforge.adapters.container_sandbox import ContainerSandbox, ContainerSandboxConfig
from loopforge.adapters.local_sandbox import CommandSpec, SandboxLimits
from loopforge.domain.security import SandboxRequirements
from loopforge.ports.sandbox import SandboxError


class FormattingError(ValueError):
    """Formatting cannot safely produce a replacement."""


class _Formatted(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    source: str = Field(max_length=65536)


def import_signature(source: str) -> tuple[str, tuple[str, ...]]:
    """Preserve non-import AST and imports' names, aliases and lexical scopes.

    This is a structural guard, not proof of arbitrary import-order equivalence.
    """
    tree = ast.parse(source)
    imports: list[str] = []

    def strip(node: ast.AST, scope: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, f"{type(node).__name__}:{node.name}")
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                values = cast("list[object]", value)
                kept: list[ast.AST] = []
                for child in values:
                    if not isinstance(child, ast.AST):
                        continue
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        imports.extend(
                            repr(
                                (
                                    scope,
                                    type(child).__name__,
                                    getattr(child, "module", None),
                                    getattr(child, "level", 0),
                                    alias.name,
                                    alias.asname,
                                )
                            )
                            for alias in child.names
                        )
                    else:
                        strip(child, scope)
                        kept.append(child)
                # Only AST-valued lists are transformed (e.g. body, not string lists).
                if all(isinstance(child, ast.AST) for child in values):
                    setattr(node, field, kept)
            elif isinstance(value, ast.AST):
                strip(value, scope)

    strip(tree, ())
    return ast.dump(tree, include_attributes=False), tuple(sorted(imports))


class PinnedImportFormatter:
    """Only I001 fixes; argv/options are fixed and repository config is ignored."""

    def __init__(self, root: Path, image: str) -> None:
        self.root, self.image = root.resolve(), image

    def format_file(self, path: str, expected_sha256: str) -> str:
        selected = Path(path)
        target = self.root / selected
        if (
            selected.is_absolute()
            or ".." in selected.parts
            or selected.suffix != ".py"
            or any(part.startswith(".") for part in selected.parts)
            or target.is_symlink()
            or not target.resolve().is_relative_to(self.root)
            or any(parent.is_symlink() for parent in target.parents if parent != self.root.parent)
        ):
            msg = "Unsafe Python formatter path"
            raise FormattingError(msg)
        raw = target.read_bytes()
        if len(raw) > 32768 or hashlib.sha256(raw).hexdigest() != expected_sha256:
            msg = "Formatter source is oversized or its fingerprint changed"
            raise FormattingError(msg)
        original = raw.decode("utf-8")
        program = (
            "import hashlib,json,subprocess,tempfile\nfrom pathlib import Path\n"
            f"raw=(Path('/workspace') / {path!r}).read_bytes()\n"
            f"assert hashlib.sha256(raw).hexdigest()=={expected_sha256!r}\n"
            "with tempfile.TemporaryDirectory(dir='/tmp') as folder:\n"
            " p=Path(folder)/'source.py';p.write_bytes(raw)\n"
            " r=subprocess.run(['/usr/local/bin/python','-I','-B','-m','ruff','check','--no-cache',"
            "'--isolated','--select','I001','--fix',str(p)],capture_output=True,text=True)\n"
            " assert r.returncode==0,r.stderr+r.stdout\n"
            " print(json.dumps({'source':p.read_text()}))\n"
        )
        sandbox = ContainerSandbox(
            self.root,
            config=ContainerSandboxConfig(
                image=self.image,
                workspace_read_only=True,
                commands=(
                    CommandSpec(
                        name="format_python_imports",
                        argv=("/usr/local/bin/python", "-I", "-B", "-c", program),
                        timeout_seconds=30,
                        cpu_seconds=30,
                    ),
                ),
                limits=SandboxLimits(max_memory_bytes=2 * 1024**3),
                environment={"HOME": "/tmp", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
            ),
        )
        sandbox.capabilities.require(
            SandboxRequirements(
                file_api_confined=True,
                symlink_protected=True,
                environment_filtered=True,
                process_timeout=True,
                resource_limits=True,
                output_limited=True,
                process_filesystem_isolated=True,
                network_isolated=True,
            )
        )
        try:
            result = sandbox.run("format_python_imports")
        except SandboxError as exc:
            msg = "Pinned import formatter sandbox failed"
            raise FormattingError(msg) from exc
        if not result.succeeded:
            msg = "Pinned import formatter failed: " + result.stderr[:1000]
            raise FormattingError(msg)
        formatted = _Formatted.model_validate_json(result.stdout).source
        if import_signature(original) != import_signature(formatted):
            msg = "Formatter changed non-import AST or import names/scopes"
            raise FormattingError(msg)
        return formatted
