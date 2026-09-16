"""Install release artifacts into clean environments and smoke-test the CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(*argv: str) -> None:
    subprocess.run(argv, check=True)


def _python_in(venv: Path) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return venv / directory / executable


def smoke(artifact: Path, *, expected_version: str) -> None:
    if not artifact.is_file():
        msg = f"artifact does not exist: {artifact}"
        raise FileNotFoundError(msg)

    with tempfile.TemporaryDirectory(prefix="loopforge-artifact-") as temporary:
        venv = Path(temporary) / "venv"
        _run("uv", "venv", "--python", "3.12", str(venv))
        python = _python_in(venv)
        _run("uv", "pip", "install", "--python", str(python), str(artifact.resolve()))
        probe = (
            "from importlib.metadata import version; "
            "from importlib.resources import files; "
            f"assert version('loopforge-console') == {expected_version!r}; "
            "assert files('loopforge').joinpath('console_static/index.html').is_file()"
        )
        _run(str(python), "-c", probe)
        _run(str(python), "-m", "loopforge.entrypoints.cli", "--help")
        print(f"smoke passed: {artifact.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()
    for artifact in args.artifacts:
        smoke(artifact, expected_version=args.expected_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
