#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -r requirements.txt

export PYTHONPATH="$REPO_ROOT/src"
# Allow override via CLAIRE_CONFIG_PATH; otherwise use default search path in code
export CLAIRE_VOICE_DEBUG=${CLAIRE_VOICE_DEBUG:-0}
# Canonical entry point: src/claire/__main__.py -> claire.cli -> claire.daemon
python -m claire
