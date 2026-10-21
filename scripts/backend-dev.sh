#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f ".venv/bin/python" ]]; then
  echo "Missing .venv. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e './backend[dev]'" >&2
  exit 1
fi
exec .venv/bin/python -m uvicorn finintel.main:app --host 127.0.0.1 --port 8765
