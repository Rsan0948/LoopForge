"""Hermetic formatter structural, sandbox-policy and path controls."""

import hashlib
import json
from pathlib import Path

import pytest

from loopforge.adapters import python_formatter as formatting
from loopforge.adapters.container_sandbox import ContainerSandbox
from loopforge.ports.sandbox import SandboxCommandResult, SandboxError

ORIGINAL = "import sys\nimport os\n\nvalue = 1\n"
FORMATTED = "import os\nimport sys\n\nvalue = 1\n"


@pytest.mark.parametrize(
    ("output", "passes"),
    [
        (FORMATTED, True),
        (FORMATTED.replace("value = 1", "value = 2"), False),
        (FORMATTED.replace("import sys\n", ""), False),
        (FORMATTED.replace("import sys", "import sys as something"), False),
    ],
)
def test_formatter_only_accepts_import_reordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str, passes: bool
) -> None:
    path = tmp_path / "source.py"
    path.write_text(ORIGINAL)
    captured: list[str] = []

    class FakeSandbox(ContainerSandbox):
        def run(self, command_name: str, *, timeout_seconds: float | None = None):
            del timeout_seconds
            captured.append(command_name)
            return SandboxCommandResult(
                exit_code=0, stdout=json.dumps({"source": output}), stderr="", succeeded=True
            )

    monkeypatch.setattr(formatting, "ContainerSandbox", FakeSandbox)
    formatter = formatting.PinnedImportFormatter(tmp_path, "sha256:" + "a" * 64)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if passes:
        assert formatter.format_file("source.py", digest) == FORMATTED
    else:
        with pytest.raises(ValueError, match=r"AST|import"):
            formatter.format_file("source.py", digest)
    assert path.read_text() == ORIGINAL  # Adapter computes a replacement; never writes host source.
    assert captured == ["format_python_imports"]


@pytest.mark.parametrize("path", ["../outside.py", "/tmp/outside.py", ".private.py", "source.txt"])
def test_formatter_rejects_unsafe_paths_before_sandbox(tmp_path: Path, path: str) -> None:
    formatter = formatting.PinnedImportFormatter(tmp_path, "sha256:" + "a" * 64)
    with pytest.raises(formatting.FormattingError, match="Unsafe"):
        formatter.format_file(path, "0" * 64)


def test_formatter_rejects_stale_oversized_and_symlink_sources(tmp_path: Path) -> None:
    path = tmp_path / "source.py"
    path.write_text(ORIGINAL)
    formatter = formatting.PinnedImportFormatter(tmp_path, "sha256:" + "a" * 64)
    with pytest.raises(formatting.FormattingError, match="fingerprint"):
        formatter.format_file("source.py", "0" * 64)
    (tmp_path / "link.py").symlink_to(path)
    with pytest.raises(formatting.FormattingError, match="Unsafe"):
        formatter.format_file("link.py", hashlib.sha256(path.read_bytes()).hexdigest())
    path.write_text("#" * 32769)
    with pytest.raises(formatting.FormattingError, match="oversized"):
        formatter.format_file("source.py", hashlib.sha256(path.read_bytes()).hexdigest())


def test_import_signature_distinguishes_lexical_scope_and_nonimport_code() -> None:
    assert formatting.import_signature(ORIGINAL) == formatting.import_signature(FORMATTED)
    assert formatting.import_signature("def f():\n    import os\n") != formatting.import_signature(
        "import os\ndef f():\n    pass\n"
    )


@pytest.mark.parametrize("failure", ["sandbox", "exit", "json", "extra"])
def test_formatter_failure_never_changes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "source.py"
    path.write_text(ORIGINAL)

    class FailingSandbox(ContainerSandbox):
        def run(self, command_name: str, *, timeout_seconds: float | None = None):
            del command_name, timeout_seconds
            if failure == "sandbox":
                msg = "unavailable"
                raise SandboxError(msg)
            return SandboxCommandResult(
                exit_code=1 if failure == "exit" else 0,
                stdout="invalid"
                if failure == "json"
                else json.dumps({"source": FORMATTED, "unexpected": True}),
                stderr="failed",
                succeeded=failure != "exit",
            )

    monkeypatch.setattr(formatting, "ContainerSandbox", FailingSandbox)
    formatter = formatting.PinnedImportFormatter(tmp_path, "sha256:" + "a" * 64)
    with pytest.raises(ValueError, match=r"formatter|validation"):
        formatter.format_file("source.py", hashlib.sha256(path.read_bytes()).hexdigest())
    assert path.read_text() == ORIGINAL
