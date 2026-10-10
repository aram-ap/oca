#!/usr/bin/env bash
# Isolated copy of production for reproducing incidents and proving fixes.
#
#   ops/sandbox.sh up [ref]        run the sandbox at a git ref (default: the sha prod runs)
#   ops/sandbox.sh loadtest [N]    fixed-size load test against the sandbox; prints JSON
#   ops/sandbox.sh reset-db        drop post-baseline indexes/migrations in the sandbox DB
#   ops/sandbox.sh down            stop the sandbox and delete its data
#
# The sandbox builds from a separate git worktree (.sandbox/), so testing a fix
# branch never touches the production checkout. It has its own containers,
# network, database volume and log dir, on ports 18080/18000/15432/16379/19090.
#
# Typical proof loop:
#   ops/sandbox.sh up                    # same code as prod -> reproduce
#   ops/sandbox.sh loadtest 2000 > before.json
#   ops/sandbox.sh up fix/INC-0042       # candidate fix
#   ops/sandbox.sh loadtest 2000 > after.json
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT="driftwood-sandbox"
WT="$ROOT/.sandbox"

compose() { (cd "$WT" && docker compose -p "$PROJECT" --env-file sandbox.env "$@"); }

prod_sha() {
  { tail -n 1 state/deployments.jsonl 2>/dev/null || true; } \
    | python3 -c 'import sys,json; l=sys.stdin.read().strip(); print(json.loads(l)["git_sha"] if l else "HEAD")'
}

case "${1:-}" in
  up)
    REF="${2:-$(prod_sha)}"
    SHA="$(git rev-parse --short=10 "$REF")"
    if [ -d "$WT" ]; then
      git -C "$WT" checkout -q --detach "$SHA"
    else
      git worktree add -q --detach "$WT" "$SHA"
    fi
    export APP_VERSION="$(tr -d '[:space:]' < "$WT/services/orders-api/VERSION")" GIT_SHA="$SHA"
    echo "sandbox -> ${APP_VERSION} (${SHA})" >&2
    compose up -d --build --wait --wait-timeout 900 >&2
    compose exec -T redis redis-cli FLUSHALL >/dev/null
    echo "{\"sandbox\": \"up\", \"git_sha\": \"${SHA}\", \"version\": \"${APP_VERSION}\"}"
    ;;
  loadtest)
    N="${2:-2000}"
    compose build -q loadgen >&2
    compose run --rm -T --no-deps loadgen \
      --base-url=http://frontend/api --requests="$N" \
      --rate="${LOADTEST_RATE:-30}" 2>/dev/null
    ;;
  reset-db)
    (cd "$WT" && PROJECT="$PROJECT" ENV_FILE=sandbox.env "$ROOT/ops/reset_db.sh")
    compose exec -T redis redis-cli FLUSHALL >/dev/null
    ;;
  down)
    [ -d "$WT" ] && compose down -v >&2
    git worktree remove --force "$WT" 2>/dev/null || true
    echo '{"sandbox": "down"}'
    ;;
  *)
    sed -n '2,20p' "$0"; exit 2 ;;
esac
