#!/usr/bin/env bash
# FinIntel local historical backfill controller. Run with: bash scripts/backfill.sh start|status|logs|stop
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="$ROOT/.venv/bin/python"
MODULE="finintel.market_data.backfill_worker"
PID_FILE="$ROOT/data/backfill.pid"
LOG_FILE="$ROOT/data/backfill.log"
STATE_FILE="$ROOT/data/finintel.backfill.json"
mkdir -p "$ROOT/data"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # Check command identity before claiming/stopping a process.
  ps -p "$pid" -o command= 2>/dev/null | grep -Fq "$MODULE"
}

check_setup() {
  if [[ ! -x "$PYTHON" ]]; then
    echo "Missing Python environment: $PYTHON" >&2
    exit 2
  fi
  if [[ ! -f "$ROOT/backend/finintel/market_data/backfill_worker.py" ]]; then
    echo "Backfill worker missing from this checkout." >&2
    echo "Fetch and switch to feat/catalog-batch-acquisition (PR #11) first." >&2
    exit 2
  fi
  if ! PYTHONPATH="$ROOT/backend" "$PYTHON" -m "$MODULE" --help >/dev/null; then
    echo "Python cannot load the backfill module. Check your backend dependencies." >&2
    exit 2
  fi
}

case "${1:-}" in
  start)
    if is_running; then
      echo "Already running (PID $(cat "$PID_FILE"))."
      exit 0
    fi
    check_setup
    # Provider and caps can be changed via environment variables, never hardcoded secrets.
    PROVIDER="${FININTEL_BACKFILL_PROVIDER:-alpaca_sip}"
    MAX_REQUESTS="${FININTEL_BACKFILL_MAX_REQUESTS_PER_DAY:-100}"
    DELAY="${FININTEL_BACKFILL_DELAY_SECONDS:-2}"
    PERIOD="${FININTEL_BACKFILL_PERIOD:-10y}"
    echo "Starting local historical backfill: $PROVIDER ($PERIOD); budget $MAX_REQUESTS requests/day."
    nohup env PYTHONPATH="$ROOT/backend" FININTEL_DB_PATH="${FININTEL_DB_PATH:-$ROOT/data/finintel.sqlite3}" \
      "$PYTHON" -u -m "$MODULE" \
      --provider "$PROVIDER" --period "$PERIOD" \
      --max-requests-per-day "$MAX_REQUESTS" --delay-seconds "$DELAY" \
      --state-path "$STATE_FILE" >> "$LOG_FILE" 2>&1 < /dev/null &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    sleep 1
    if is_running; then
      echo "Running as PID $pid. Logs: $LOG_FILE"
    else
      echo "Worker exited. Check: tail -40 '$LOG_FILE'" >&2
      rm -f "$PID_FILE"
      exit 1
    fi
    ;;
  status)
    if is_running; then
      echo "RUNNING (PID $(cat "$PID_FILE"))"
    else
      echo "NOT RUNNING"
    fi
    if [[ -f "$STATE_FILE" ]]; then
      if [[ -x "$PYTHON" ]]; then
        "$PYTHON" -c 'import json,sys; p=json.load(open(sys.argv[1])); print("Provider:",p.get("provider")," Status:",p.get("last_status")," Requests today:",p.get("requests")," Current:",p.get("current_symbol"))' "$STATE_FILE"
      else
        cat "$STATE_FILE"
      fi
    else
      echo "No checkpoint yet."
    fi
    ;;
  logs)
    touch "$LOG_FILE"
    tail -n 50 -f "$LOG_FILE"
    ;;
  stop)
    if is_running; then
      pid="$(cat "$PID_FILE")"
      kill "$pid"
      echo "Stop requested for PID $pid; safe restart resumes from checkpoint."
    else
      echo "No worker is running."
    fi
    rm -f "$PID_FILE"
    ;;
  *)
    echo "Usage: bash scripts/backfill.sh {start|status|logs|stop}" >&2
    echo "Optional env: FININTEL_BACKFILL_PROVIDER=alpaca_sip FININTEL_BACKFILL_MAX_REQUESTS_PER_DAY=100 FININTEL_BACKFILL_DELAY_SECONDS=2 FININTEL_BACKFILL_PERIOD=10y" >&2
    exit 2
    ;;
esac
