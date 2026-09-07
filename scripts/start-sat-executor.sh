#!/usr/bin/env bash
# Script to launch sat_executor background process

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

echo "[*] Starting sat_executor server..."
python3 -m sat_executor "$@"
