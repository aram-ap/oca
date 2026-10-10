#!/usr/bin/env bash
# Put production back to the healthy baseline (tag: baseline) between demo runs.
#
# - resets main to the baseline tag (discards incident merges and agent fixes)
# - drops any index or migration added after the baseline, so a fix that added
#   an index doesn't make the next run of the loyalty incident harmless
# - flushes Redis caches and carts
# - redeploys and records the deploy
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT="${PROJECT:-driftwood}"
ENV_FILE="${ENV_FILE:-.env}"

git checkout -q main
git reset -q --hard baseline
git branch --list 'fix/*' | xargs -r git branch -q -D

ops/reset_db.sh
docker compose -p "$PROJECT" --env-file "$ENV_FILE" exec -T redis redis-cli FLUSHALL >/dev/null || true
ops/deploy.sh "reset to baseline"
