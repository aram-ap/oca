#!/usr/bin/env bash
# Inject an incident by shipping a realistic bad change through the normal deploy path.
#
#   ops/inject.sh timeout    merge perf/fail-fast-db-timeouts  (config change, v1.3.1)
#   ops/inject.sh loyalty    merge feature/loyalty-tiers       (new feature, v1.4.0)
#
# Nothing here is "fake failure" code: each incident is an ordinary-looking commit
# by a teammate, merged and deployed. The agents only see git, logs, metrics and
# the database, exactly as they would in a real company.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

case "${1:-}" in
  timeout) BRANCH="perf/fail-fast-db-timeouts"; WHO="Priya Nair <priya@driftwood.dev>" ;;
  loyalty) BRANCH="feature/loyalty-tiers";      WHO="Marcus Bell <marcus@driftwood.dev>" ;;
  *) echo "usage: ops/inject.sh timeout|loyalty" >&2; exit 2 ;;
esac

# Incident branches live in this repo; in a fresh clone they're only on origin.
git rev-parse -q --verify "refs/heads/${BRANCH}" >/dev/null || BRANCH="origin/${BRANCH}"

git checkout -q main
NAME="${WHO% <*}"; EMAIL="${WHO#*<}"; EMAIL="${EMAIL%>}"
GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" \
GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL" \
  git merge --no-ff --no-edit -q "$BRANCH" -m "Merge branch '${BRANCH#origin/}'"

ops/deploy.sh
echo
echo "incident '${1}' is live. Watch it with: python3 ops/status.py --watch"
