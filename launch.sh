#!/usr/bin/env bash
# CMM-Gen launcher (macOS/Linux) -- sets up a virtual environment, installs
# dependencies, runs a smoke test against the bundled sample part, and
# launches the Streamlit visualizer.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "============================================"
echo " CMM-Gen setup + launch"
echo "============================================"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "[ERROR] $PYTHON_BIN not found. Install Python 3.11+ and re-run." >&2
    exit 1
fi
echo "Found $("$PYTHON_BIN" --version)"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment in .venv ..."
    "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing dependencies (first run only takes a few minutes) ..."
python -m pip install --upgrade pip >/dev/null
if ! pip install -r requirements.txt; then
    echo "[ERROR] Dependency install failed -- see the error above." >&2
    echo "        cadquery/OCP is the most likely culprit; it needs a" >&2
    echo "        64-bit Python 3.11 or 3.12. Check your Python version and retry." >&2
    exit 1
fi

echo
echo "---- Smoke test: parsing the bundled sample part ----"
python -m cmm_gen.cli parse-cad --step tests/fixtures/sample_part.step

echo
echo "Smoke test passed. Launching the visualizer at http://localhost:8501 ..."
echo "(Press Ctrl+C to stop it.)"
echo
python -m cmm_gen.cli visualize
