#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
else
  echo "Python 3 was not found in PATH." >&2
  exit 1
fi

"$PYTHON_CMD" -m venv .venv

if [[ -f .venv/Scripts/activate ]]; then
  # Windows Git Bash
  source .venv/Scripts/activate
  ACTIVATE_PATH=".venv/Scripts/activate"
elif [[ -f .venv/bin/activate ]]; then
  # Linux / WSL
  source .venv/bin/activate
  ACTIVATE_PATH=".venv/bin/activate"
else
  echo "Virtual environment activation script was not created." >&2
  exit 1
fi

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Setup complete. Activate with: source $ACTIVATE_PATH"
