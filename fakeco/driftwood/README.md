# Driftwood Coffee Co.

```
 loadgen (customers, ~30 visits/s)
    │
    ▼
 frontend  nginx :8080 ──/api──► orders-api  FastAPI :8000 ──► postgres :5432  (2M orders)
                                     │                    └──► redis    :6379  (carts, caches)
                                     ▼
                              /metrics ◄── prometheus :9090
                              JSON logs ──► logs/orders-api.log, logs/frontend.log
 git history + state/deployments.jsonl = what changed and when
```

## Quick start

```bash
docker compose build                         # needs internet once (see "Going offline")
ops/deploy.sh "initial deploy"               # first boot seeds 2M orders: ~1–2 min
python3 ops/status.py --watch                # healthy: 0% errors, p95 ≈ 10–20 ms
open http://localhost:8080                   # the storefront
```

## What the agents can read (and how)

| Source | Where | Notes |
|---|---|---|
| Metrics | Prometheus `localhost:9090` | Recorded series: `orders_api:error_rate:1m`, `orders_api:p95_latency_seconds:1m`, `orders_api:db_pool_utilization`, `orders_api:db_query_p95_seconds:1m` (per named query), `orders_api:db_errors:rate1m`. Raw: `http_requests_total`, `db_query_duration_seconds`, `db_pool_in_use`, `app_info{version,git_sha}` |
| App logs | `logs/orders-api.log` | One JSON object per line: `ts, level, msg, route, status, duration_ms, request_id`, plus `query` on DB errors |
| Edge logs | `logs/frontend.log` | nginx JSON access log, same `request_id` |
| Deploy history | `state/deployments.jsonl` | `deploy_id, ts, git_sha, prev_sha, version, author, subject, files_changed` |
| Code & diffs | this git repo | `git diff <prev_sha> <git_sha>` for any deploy |
| Database | `postgresql://sre_readonly:sre_readonly@localhost:5432/driftwood` | Read-only role. `pg_stat_statements` and `pg_stat_activity` are enabled |
| Health snapshot | `python3 ops/status.py --json` | The deterministic detector. `--check` exits 1 on an SLO breach (5xx > 5%, p95 > 1 s, pool > 90%) |

## The incidents

Inject one with `ops/inject.sh <name>`. It merges a teammate's branch into `main`
and deploys it through `ops/deploy.sh`, so it shows up in git and the deploy history
like any other change.

### `timeout`: a config change (the easy one)

`perf/fail-fast-db-timeouts`, by Priya, v1.3.1. Sets `DB_TIMEOUT_MS` from 3000 to 100.
The commit says all hot-path queries take single-digit milliseconds. They do, except
the homepage bestsellers aggregate, which takes about 800 ms on the test box.

- **Symptoms:** `/bestsellers` fails 100% and everything else is fine. Logs show
  `database request timeout` with `query: bestsellers_30d` and `db_timeout_ms: 100`.
  Bestsellers requests queue behind the cache refresh, so p95 climbs into seconds.
- **Root cause:** deploy N changed `config/app.env`.
- **Fix:** revert the value, or set a per-query timeout for the report query.

### `loyalty`: a feature that needs a forward fix (the one that impresses)

`feature/loyalty-tiers`, by Marcus, v1.4.0. Adds tiered discounts. The tier shows in
the header on every page through `/session`, and checkout applies it. Its tests pass.
But the tier lookup filters `orders` by `customer_email`, which has no index, so every
page view sequential-scans 2 million rows.

- **Symptoms:** everything degrades at once. The DB pool is exhausted, there are 503s
  on unrelated endpoints, and `/bestsellers` and checkout both slow down. The loudest
  log line is `database connection pool exhausted`, which points away from the cause.
- **Root cause:** `orders_api:db_query_p95_seconds:1m{query="loyalty_lifetime_orders"}`
  and `pg_stat_statements` show the new query dominating DB time. `EXPLAIN` shows
  `Seq Scan on orders`.
- **Wrong fix:** rolling back. It "works" but deletes a shipped feature.
- **Right fix:** a migration such as
  `services/orders-api/migrations/002_orders_customer_email_idx.sql` containing
  `CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_customer_email ON orders (customer_email);`.
  The API applies new migrations on startup. Keep `CONCURRENTLY` statements alone in
  their own file, because they can't run inside a transaction. The Guardian should see
  "database migration: YES" and treat the fix as higher risk than a config change.

### Measured results

From a 2-core test box with the load generator on the same machine, using
`--rate 30` and about 3,000 requests. The GX10 is much faster, so expect lower
latencies; see "Calibrating".

| Scenario | Error rate | p50 | p95 | Notes |
|---|---|---|---|---|
| Baseline v1.3.0 | 0% | 5 ms | 10 ms | |
| `timeout` injected | 25% | 23 ms | 10 s | bestsellers 100% failing, rest fine |
| `loyalty` injected | 47% | 5.5 s | 15 s | every endpoint degraded, pool exhausted |
| `loyalty` + index fix | 0% | 6 ms | 16 ms | feature kept; lookup 1,190 ms → 1.2 ms |

## Sandbox: reproduce, then prove the fix

```bash
ops/sandbox.sh up                         # sandbox runs the same sha as prod
ops/sandbox.sh loadtest 3000 > before.json
git checkout -b fix/INC-0042 && <edit> && git commit -am "..." && git checkout main
ops/sandbox.sh up fix/INC-0042
ops/sandbox.sh loadtest 3000 > after.json
API_URL=http://localhost:18000 python3 -m pytest services/orders-api/tests -q
ops/sandbox.sh reset-db                   # before re-testing old code after an index fix
```

The sandbox is a second compose project (`driftwood-sandbox`) built from a git
worktree in `.sandbox/`, with its own database and logs and ports offset by 10000.
Production is never touched. It seeds its own copy of the data on first boot.

To ship a fix that passed: `git merge --no-ff fix/INC-0042 && ops/deploy.sh "INC-0042"`.
Your command gate is the thing that should be allowed to run that line.

## Reset between demo runs

```bash
ops/reset.sh          # main → baseline tag, drops post-baseline indexes, flushes Redis, redeploys
```

## Calibrating

Incident severity depends on hardware. The loyalty incident needs enough traffic to
saturate the DB. If `ops/status.py` doesn't show a breach within about 60 s of
`ops/inject.sh loyalty`, raise `LOADGEN_RATE` in `.env` (try 60, then 100) and run
`docker compose up -d loadgen`, or raise `SEED_ORDERS` (this requires recreating the
volume). Find the rate where baseline stays at 0% errors and the incident clearly
breaches. Rehearse that combination and don't change it afterwards.

## Going offline (do this before demo day)

The Docker builds `pip install` packages, and the base images come from Docker Hub.
On the GX10, with internet access:

```bash
docker compose build && docker compose -p driftwood-sandbox --env-file sandbox.env build
docker compose pull postgres redis prometheus frontend
```

After that, deploys reuse cached layers. A deploy only changes code or config layers,
not `requirements.txt`, so it never needs the network. Test it with the network
unplugged. All images used here have arm64 builds.

## Layout

```
services/orders-api/   app/ (main, db, config, telemetry), config/app.env, migrations/, tests/
services/frontend/     nginx.conf, html/index.html
services/loadgen/      loadgen.py (continuous traffic, or --requests N for a fixed test)
db/init/               schema + seed (first boot only)
prometheus/            scrape config + recording rules/alerts
ops/                   deploy, inject, reset, reset_db, sandbox, status
```
