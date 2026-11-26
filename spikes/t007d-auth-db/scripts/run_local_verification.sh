#!/usr/bin/env bash
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.13}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN="python3"
fi

VENV_DIR="$(mktemp -d "${TMPDIR:-/tmp}/t007d-venv.XXXXXX")"
trap 'rm -rf "$VENV_DIR"' EXIT

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$SPIKE_DIR/requirements.txt"

echo "Running T-007D local Auth/DB isolation verification suite..."
"$VENV_DIR/bin/pytest" -v "$SPIKE_DIR/tests"
