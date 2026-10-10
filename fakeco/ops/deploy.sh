#!/usr/bin/env bash
# Deploy the current git HEAD to production and record it in the deploy history.
#
#   ops/deploy.sh                 deploy HEAD to production
#   ops/deploy.sh "reason text"   same, with a note stored in the deploy record
#
# Every deploy appends one JSON line to state/deployments.jsonl:
#   deploy_id, ts, git_sha, prev_sha, version, author, subject, files_changed, note
# That file plus `git log` / `git diff <prev_sha> <git_sha>` is the deploy history
# the agents investigate.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT="${PROJECT:-driftwood}"
ENV_FILE="${ENV_FILE:-.env}"
HISTORY="state/deployments.jsonl"
mkdir -p state logs

if [ -n "$(git status --porcelain -- services db prometheus docker-compose.yml)" ]; then
  echo "refusing to deploy: uncommitted changes under services/, db/ or prometheus/" >&2
  exit 1
fi

SHA="$(git rev-parse --short=10 HEAD)"
PREV_SHA="$({ tail -n 1 "$HISTORY" 2>/dev/null || true; } | python3 -c 'import sys,json; l=sys.stdin.read().strip(); print(json.loads(l)["git_sha"] if l else "")')"
export APP_VERSION="$(tr -d '[:space:]' < services/orders-api/VERSION)"
export GIT_SHA="$SHA"

echo "deploying ${APP_VERSION} (${SHA}) to ${PROJECT}..."
docker compose -p "$PROJECT" --env-file "$ENV_FILE" up -d --build --wait --wait-timeout 600

python3 - "$HISTORY" "$SHA" "$PREV_SHA" "$APP_VERSION" "${1:-}" <<'PY'
import json, subprocess, sys, time
history, sha, prev, version, note = sys.argv[1:6]
git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
try:
    with open(history) as f:
        last_id = max((json.loads(l)["deploy_id"] for l in f if l.strip()), default=0)
except FileNotFoundError:
    last_id = 0
files = git("diff", "--name-only", f"{prev}..{sha}").splitlines() if prev and prev != sha else []
record = {
    "deploy_id": last_id + 1,
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "git_sha": sha,
    "prev_sha": prev or None,
    "version": version,
    "author": git("log", "-1", "--format=%an <%ae>", sha),
    "subject": git("log", "-1", "--format=%s", sha),
    "files_changed": files,
    "note": note or None,
}
with open(history, "a") as f:
    f.write(json.dumps(record) + "\n")
print(f"deploy #{record['deploy_id']} recorded: {version} {sha} — {record['subject']}")
PY
