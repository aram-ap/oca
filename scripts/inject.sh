#!/usr/bin/env bash
# Inject a demo incident into the running Driftwood company so OCA detects and fixes it.
#
#   scripts/inject.sh                reset to baseline, then inject the `timeout` incident
#   scripts/inject.sh timeout        config regression: DB_TIMEOUT_MS 3000 -> 100 (v1.3.1)
#   scripts/inject.sh loyalty        new feature with an unindexed query (v1.4.0)
#   scripts/inject.sh --no-reset <s> inject without resetting first (re-run only if branch not merged)
#
# This is the documented demo beat (README section 10): the incident is an ordinary teammate
# commit shipped through the company's own deploy path. It touches only the Driftwood demo stack,
# never production of anything else. OCA must already be running (scripts/setup.sh).
set -euo pipefail

OCA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OCA_DEMO_ENV="${OCA_DEMO_ENV:-$HOME/.config/oca/demo.env}"
[ -f "$OCA_DEMO_ENV" ] && { set -a; . "$OCA_DEMO_ENV"; set +a; }

OCA_FAKECO_ROOT="${OCA_FAKECO_ROOT:-$HOME/FakeCo_Driftwood}"
OCA_API_URL="${OCA_API_URL:-http://localhost:${OCA_PORT:-8001}}"
DRIFTWOOD="$OCA_FAKECO_ROOT"

DO_RESET=1 SCENARIO=""
for arg in "$@"; do
  case "$arg" in
    --no-reset)      DO_RESET=0 ;;
    timeout|loyalty) SCENARIO="$arg" ;;
    -h|--help)       sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "usage: inject.sh [--no-reset] [timeout|loyalty]" >&2; exit 2 ;;
  esac
done
SCENARIO="${SCENARIO:-timeout}"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ -x "$DRIFTWOOD/ops/inject.sh" ] || fail "$DRIFTWOOD/ops/inject.sh not found (set OCA_FAKECO_ROOT)"

log "Checking OCA is up"
curl -sf "$OCA_API_URL/api/health" >/dev/null \
  || fail "OCA not reachable at $OCA_API_URL — run scripts/setup.sh first"
echo "$(curl -s "$OCA_API_URL/api/health")"

if [ "$DO_RESET" = 1 ]; then
  log "Resetting Driftwood to baseline (clean slate for a fresh run)"
  ( cd "$DRIFTWOOD" && ops/reset.sh )
fi

log "Injecting '$SCENARIO' incident"
( cd "$DRIFTWOOD" && ops/inject.sh "$SCENARIO" )

log "Incident '$SCENARIO' is live"
echo "OCA should open a ticket within one check interval. Watch it:"
echo "  $OCA_API_URL/dash       (dashboard)"
echo "  $OCA_API_URL/logs       (event log)"
