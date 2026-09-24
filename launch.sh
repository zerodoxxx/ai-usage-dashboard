#!/usr/bin/env bash
# launch.sh — One-shot setup & launch for AI Tools Usage & Cost Visualizer
# Usage: bash launch.sh [--port 8765] [--no-browser]
set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_BASE="/opt/homebrew/Caskroom/miniconda/base"
ENV_NAME="ai-usage-dashboard"
PYTHON="$CONDA_BASE/envs/$ENV_NAME/bin/python"
PIP="$CONDA_BASE/envs/$ENV_NAME/bin/pip"
CONDA_EXE="$CONDA_BASE/bin/conda"
PORT=8765
OPEN_BROWSER=true

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)    PORT="$2"; shift 2 ;;
    --no-browser) OPEN_BROWSER=false; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ─── Colours ──────────────────────────────────────────────────────────────────
CYAN='\033[96m'; GREEN='\033[92m'; YELLOW='\033[93m'; RED='\033[91m'; RESET='\033[0m'; BOLD='\033[1m'
info()    { echo -e "${CYAN}[•]${RESET} $*"; }
success() { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
error()   { echo -e "${RED}[✗]${RESET} $*" >&2; exit 1; }

is_current_dashboard_process() {
  local listener_pid="$1"
  local process_cwd process_command
  local -a process_args

  process_cwd="$(lsof -a -p "$listener_pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  [[ "$process_cwd" == "$SCRIPT_DIR" ]] || return 1

  process_command="$(ps -p "$listener_pid" -o command= 2>/dev/null || true)"
  read -r -a process_args <<< "$process_command"
  [[ "${process_args[0]:-}" == "$PYTHON" && \
    "${process_args[1]:-}" == "run.py" && \
    "${process_args[2]:-}" == "--port" && \
    "${process_args[3]:-}" == "$PORT" ]]
}

echo ""
echo -e "${CYAN}${BOLD}╔═══════════════════════════════════════════════╗"
echo -e "║   AI Tools Usage & Cost Visualizer           ║"
echo -e "║   Launch Script                               ║"
echo -e "╚═══════════════════════════════════════════════╝${RESET}"
echo ""

# ─── Step 1: Change to project directory ─────────────────────────────────────
info "Working directory: $SCRIPT_DIR"
cd "$SCRIPT_DIR"

# ─── Step 2: Verify conda is available ───────────────────────────────────────
if [[ ! -f "$CONDA_EXE" ]]; then
  error "Conda not found at $CONDA_EXE. Is miniconda installed at $CONDA_BASE?"
fi
success "Conda found: $CONDA_EXE"

# ─── Step 3: Create conda env if missing ─────────────────────────────────────
if "$CONDA_EXE" env list | grep -q "^$ENV_NAME "; then
  success "Conda env '$ENV_NAME' already exists"
else
  info "Creating conda environment '$ENV_NAME' with Python 3.12..."
  "$CONDA_EXE" create -n "$ENV_NAME" python=3.12 -y -q
  success "Conda env '$ENV_NAME' created"
fi

# ─── Step 4: Install / sync dependencies ─────────────────────────────────────
if [[ -f "requirements.txt" ]]; then
  info "Installing/syncing dependencies from requirements.txt..."
  "$PIP" install -q -r requirements.txt
  success "Dependencies installed"
else
  warn "requirements.txt not found — skipping pip install"
fi

# ─── Step 5: Quick sanity check on the parsers ───────────────────────────────
info "Running parser sanity check..."
if "$PYTHON" -c "from src.parsers.aggregator import get_tool_usage; d = get_tool_usage('all'); assert d.get('tool') == 'all'" 2>/dev/null; then
  success "Parser check passed (data found)"
else
  warn "Parser check could not verify data — continuing anyway"
fi

# ─── Step 6: Check if port is already in use ─────────────────────────────────
LISTENER_PIDS="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | sort -u || true)"
if [[ -n "$LISTENER_PIDS" ]]; then
  PROJECT_LISTENER=true
  for LISTENER_PID in $LISTENER_PIDS; do
    if ! is_current_dashboard_process "$LISTENER_PID"; then
      PROJECT_LISTENER=false
      break
    fi
  done

  if [[ "$PROJECT_LISTENER" == true ]]; then
    info "Stopping the existing dashboard so this launch uses the current source files..."
    for LISTENER_PID in $LISTENER_PIDS; do
      kill -TERM "$LISTENER_PID" 2>/dev/null || true
    done
    for WAIT_ATTEMPT in $(seq 1 50); do
      if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t &>/dev/null; then
        break
      fi
      sleep 0.1
    done
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t &>/dev/null; then
      error "The existing dashboard did not stop listening on port $PORT."
    fi
    success "Existing dashboard stopped"
  else
    warn "Port $PORT is already in use by another process."
    warn "Visit http://127.0.0.1:$PORT or choose a different port with --port."
    if $OPEN_BROWSER; then
      info "Opening the existing service..."
      open "http://127.0.0.1:$PORT" 2>/dev/null || true
    fi
    exit 0
  fi
fi

# ─── Step 7: Open browser after a short delay ────────────────────────────────
if $OPEN_BROWSER; then
  (sleep 1.5 && open "http://127.0.0.1:$PORT" 2>/dev/null || true) &
  success "Browser will open at http://127.0.0.1:$PORT"
fi

# ─── Step 8: Launch the dashboard ────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}Starting dashboard on http://127.0.0.1:$PORT${RESET}"
echo -e "${CYAN}Press Ctrl+C to stop.${RESET}"
echo ""

exec "$PYTHON" run.py --port "$PORT" --no-reload
