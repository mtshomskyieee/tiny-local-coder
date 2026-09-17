#!/usr/bin/env bash
# Run the unit tests on this host (no Docker, no Ollama — the suite never
# calls the model). Bootstraps a local .venv on first run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="${TLC_TEST_VENV:-$ROOT/.venv}"
PY="$VENV/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "Creating test venv at $VENV …"
  python3 -m venv "$VENV"
fi

# pytest is deliberately not in requirements.txt / the app image; it lives
# here so the test run stays self-contained.
if ! "$PY" -c "import pytest, tinylocalcoder" >/dev/null 2>&1; then
  echo "Installing test dependencies …"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet pytest -e .
fi

exec "$PY" -m pytest "${@:-tests}" -q
