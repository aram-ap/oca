#!/usr/bin/env bash
# Full demo setup: bring the Driftwood company up at a clean baseline and start OCA in real mode.
#
#   scripts/setup.sh                 reset Driftwood to baseline, (re)start OCA, wait for both healthy
#   scripts/setup.sh --no-reset      leave the company's current state alone, just ensure it is up
#   scripts/setup.sh --no-oca        set up the company only, do not touch the OCA process
#
# Everything is configurable by environment variable; the defaults match the GX10 box. Secrets
# (Jira creds, a venv on PATH) stay out of git: if ${OCA_DEMO_ENV} exists it is sourced first.
#
# This does not inject any incident. Run scripts/inject.sh for that once setup reports healthy.
set -euo pipefail

OCA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$OCA_ROOT"

# ---- configuration (override via env) ---------------------------------------
OCA_DEMO_ENV="${OCA_DEMO_ENV:-$HOME/.config/oca/demo.env}"
[ -f "$OCA_DEMO_ENV" ] && { echo "sourcing $OCA_DEMO_ENV"; set -a; . "$OCA_DEMO_ENV"; set +a; }

# A project venv on PATH (the box keeps OCA's deps in ~/venv), so `python` resolves to it.
OCA_VENV="${OCA_VENV:-$HOME/venv}"
[ -d "$OCA_VENV/bin" ] && PATH="$OCA_VENV/bin:$PATH"
PY="${PYTHON:-$(command -v python || command -v python3)}"

export OCA_RUNNER="${OCA_RUNNER:-real}"
export OCA_FAKECO_ROOT="${OCA_FAKECO_ROOT:-$HOME/FakeCo_Driftwood}"
export OCA_HOST="${OCA_HOST:-0.0.0.0}"
export OCA_PORT="${OCA_PORT:-8001}"
export OCA_API_URL="${OCA_API_URL:-http://localhost:$OCA_PORT}"
export OCA_DATA_DIR="${OCA_DATA_DIR:-$OCA_ROOT/data/live}"
export OCA_DB_PATH="${OCA_DB_PATH:-$OCA_DATA_DIR/oca.db}"
export OCA_MODEL="${OCA_MODEL:-nemotron3:33b}"
export OCA_MODEL_KEEP_ALIVE="${OCA_MODEL_KEEP_ALIVE:-30m}"
export OCA_LLM_TIMEOUT_S="${OCA_LLM_TIMEOUT_S:-600}"
export OCA_LOADTEST_REQUESTS="${OCA_LOADTEST_REQUESTS:-1500}"
export OCA_CHECK_INTERVAL_S="${OCA_CHECK_INTERVAL_S:-10}"
export OCA_STAGE_DELAY_S="${OCA_STAGE_DELAY_S:-10}"
export OCA_VERIFY_RUNS="${OCA_VERIFY_RUNS:-3}"
export OCA_VERIFY_INTERVAL_S="${OCA_VERIFY_INTERVAL_S:-20}"
export OCA_TIMEZONE="${OCA_TIMEZONE:-America/Los_Angeles}"
export OCA_HARDWARE="${OCA_HARDWARE:-ASUS Ascent GX10}"

DRIFTWOOD="$OCA_FAKECO_ROOT"
SERVER_LOG="${OCA_SERVER_LOG:-$OCA_DATA_DIR/server.log}"
DO_RESET=1 DO_OCA=1
for arg in "$@"; do
  case "$arg" in
    --no-reset) DO_RESET=0 ;;
    --no-oca)   DO_OCA=0 ;;
    -h|--help)  sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---- 1. preflight -----------------------------------------------------------
log "Preflight checks"
command -v docker >/dev/null || fail "docker not found"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 not found"
command -v git >/dev/null || fail "git not found"
[ -n "$PY" ] || fail "no python interpreter found"
"$PY" -c 'import fastapi, aiosqlite, httpx' 2>/dev/null \
  || fail "OCA deps missing for $PY (pip install -r requirements.txt, or set OCA_VENV)"
[ -d "$DRIFTWOOD/.git" ] || fail "Driftwood clone not at $DRIFTWOOD (set OCA_FAKECO_ROOT)"
[ -x "$DRIFTWOOD/ops/deploy.sh" ] || fail "$DRIFTWOOD/ops/deploy.sh missing or not executable"
case "$OCA_DATA_DIR" in "$DRIFTWOOD"/*) fail "OCA_DATA_DIR must be outside the company checkout";; esac
mkdir -p "$OCA_DATA_DIR"
echo "python:    $PY ($("$PY" -V 2>&1))"
echo "driftwood: $DRIFTWOOD"
echo "oca:       $OCA_API_URL  runner=$OCA_RUNNER  db=$OCA_DB_PATH"

# ---- 2. company to a clean, healthy baseline --------------------------------
if [ "$DO_RESET" = 1 ]; then
  log "Resetting Driftwood to baseline and deploying"
  ( cd "$DRIFTWOOD" && ops/reset.sh )
else
  log "Ensuring Driftwood is up (--no-reset: current state kept)"
  ( cd "$DRIFTWOOD" && ops/deploy.sh "setup.sh ensure up" )
fi

# ---- 3. OCA process ---------------------------------------------------------
if [ "$DO_OCA" = 1 ]; then
  log "Starting OCA"
  if PID="$(pgrep -f 'python.* -m oca.main' || true)"; [ -n "$PID" ]; then
    echo "stopping existing OCA (pid $PID)"; kill $PID 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 $PID 2>/dev/null || break; sleep 1; done
    kill -9 $PID 2>/dev/null || true
  fi
  echo "launching: $PY -m oca.main  (log: $SERVER_LOG)"
  nohup "$PY" -m oca.main >>"$SERVER_LOG" 2>&1 </dev/null &
  disown || true

  log "Waiting for OCA health"
  for i in $(seq 1 60); do
    if curl -sf "$OCA_API_URL/api/health" >/dev/null; then
      echo "healthy after ${i}s: $(curl -s "$OCA_API_URL/api/health")"; break
    fi
    [ "$i" = 60 ] && fail "OCA did not become healthy; see $SERVER_LOG"
    sleep 1
  done
else
  log "Skipping OCA (--no-oca)"
fi

log "Setup complete"
echo "Dashboard: $OCA_API_URL/dash   Inject an incident: scripts/inject.sh [timeout|loyalty]"
