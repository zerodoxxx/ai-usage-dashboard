#!/usr/bin/env bash
# launch_background.sh — Start the dashboard and detach it from this terminal.
# Usage: bash launch_background.sh [--port 8765] [--no-browser]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_HOME="${HOME:?HOME must be set}"
STATE_DIR="${AI_USAGE_DASHBOARD_STATE_DIR:-$USER_HOME/.ai-usage-dashboard}"
PID_FILE="$STATE_DIR/dashboard.pid"
LOG_FILE="$STATE_DIR/dashboard.log"
PORT=8765
OPEN_BROWSER=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      [[ $# -ge 2 ]] || { echo "Missing value for --port" >&2; exit 2; }
      PORT="$2"
      shift 2
      ;;
    --no-browser)
      OPEN_BROWSER=false
      shift
      ;;
    --stop)
      STOP=true
      shift
      ;;
    --status)
      STATUS=true
      shift
      ;;
    --log)
      tail -f "$LOG_FILE"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

STOP="${STOP:-false}"
STATUS="${STATUS:-false}"

mkdir -p "$STATE_DIR"

running_pid() {
  if [[ -s "$PID_FILE" ]]; then
    local pid
    pid="$(<"$PID_FILE")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
    rm -f "$PID_FILE"
  fi
  return 1
}

if $STATUS; then
  if pid="$(running_pid)"; then
    echo "Dashboard is running (PID $pid) at http://127.0.0.1:$PORT"
    echo "Log: $LOG_FILE"
  else
    echo "Dashboard is not running"
    exit 1
  fi
  exit 0
fi

if $STOP; then
  if pid="$(running_pid)"; then
    kill "$pid"
    for _ in {1..20}; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "Dashboard did not stop cleanly (PID $pid); use: kill $pid" >&2
      exit 1
    fi
    rm -f "$PID_FILE"
    echo "Dashboard stopped"
  else
    echo "Dashboard is not running"
  fi
  exit 0
fi

if pid="$(running_pid)"; then
  echo "Dashboard is already running (PID $pid) at http://127.0.0.1:$PORT"
  $OPEN_BROWSER && open "http://127.0.0.1:$PORT" 2>/dev/null || true
  exit 0
fi

if lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "Port $PORT is already in use by another process." >&2
  exit 1
fi

: > "$LOG_FILE"
echo "Starting dashboard in the background..."
echo "Logs: $LOG_FILE"

# nohup ignores terminal hangups; disown removes the job from this shell's job table.
# launch.sh uses exec for the server, so this PID remains the dashboard process.
nohup "$SCRIPT_DIR/launch.sh" --port "$PORT" --no-browser >>"$LOG_FILE" 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$PID_FILE"
disown "$pid" 2>/dev/null || true

for _ in {1..120}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Dashboard failed to start. See $LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl --silent --fail --max-time 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "Dashboard is running (PID $pid) at http://127.0.0.1:$PORT"
    if $OPEN_BROWSER; then
      open "http://127.0.0.1:$PORT" 2>/dev/null || true
    fi
    exit 0
  fi
  sleep 0.5
done

echo "Dashboard did not become ready within 60 seconds. See $LOG_FILE" >&2
exit 1
