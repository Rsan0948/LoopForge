#!/usr/bin/env bash
# LoopForge one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/Rsan0948/LoopForge/main/install.sh | bash
#
# What it does (and nothing more):
#   1. checks for Python 3.12+
#   2. installs pipx if it is missing (via `python3 -m pip install --user pipx`)
#   3. `pipx install loopforge-console` from PyPI
#   4. prints the one command to run next
set -euo pipefail

echo "LoopForge installer"
echo

# 1. Python 3.12+
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
    echo "error: python3 not found."
    echo "Install Python 3.12+ from https://www.python.org/downloads/ and re-run this script."
    exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    echo "error: Python 3.12+ is required; found: $("$PYTHON" --version 2>&1)"
    echo "Install a newer Python from https://www.python.org/downloads/ and re-run this script."
    exit 1
fi
echo "found $("$PYTHON" --version 2>&1)"

# 2. pipx (isolated app installs; https://pipx.pypa.io)
if command -v pipx >/dev/null 2>&1; then
    PIPX=(pipx)
else
    echo "installing pipx..."
    "$PYTHON" -m pip install --user --quiet pipx
    PIPX=("$PYTHON" -m pipx)
fi
"${PIPX[@]}" ensurepath >/dev/null 2>&1 || true

# 3. install LoopForge from PyPI
echo "installing loopforge-console..."
"${PIPX[@]}" install loopforge-console

# 4. next step
echo
echo "Done. Now type:"
echo
echo "    loopforge console"
echo
echo "That opens the LoopForge console in your browser (Ctrl-C to stop)."
echo "If 'loopforge' is not found, open a new terminal window and try again."
