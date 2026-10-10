#!/usr/bin/env python3
"""Current health of the orders API, from Prometheus. Standard library only.

    python3 ops/status.py                  one snapshot, human readable
    python3 ops/status.py --json           one snapshot as JSON (for the detector/agents)
    python3 ops/status.py --watch          refresh every 5 seconds
    python3 ops/status.py --check          exit 1 if any SLO threshold is breached
    python3 ops/status.py --prom http://localhost:19090   (the sandbox)

This is the deterministic detection layer: thresholds, not an LLM.
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

THRESHOLDS = {
    "error_rate_pct": 5.0,       # 5xx share of requests, last 1m
    "p95_latency_ms": 1000.0,    # last 1m
    "db_pool_utilization_pct": 90.0,
}

QUERIES = {
    "rps": "orders_api:rps:1m",
    "error_rate_pct": "100 * orders_api:error_rate:1m",
    "p95_latency_ms": "1000 * orders_api:p95_latency_seconds:1m",
    "db_pool_utilization_pct": "100 * orders_api:db_pool_utilization",
    "db_errors_per_s": "sum(orders_api:db_errors:rate1m) or vector(0)",
}


def query(prom: str, expr: str):
    url = f"{prom}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)["data"]["result"]


def scalar(prom: str, expr: str):
    result = query(prom, expr)
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if value != value else round(value, 2)  # NaN -> None


def snapshot(prom: str) -> dict:
    snap = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for name, expr in QUERIES.items():
        snap[name] = scalar(prom, expr)
    info = query(prom, "app_info")
    snap["version"] = info[0]["metric"].get("version") if info else None
    snap["git_sha"] = info[0]["metric"].get("git_sha") if info else None
    slow = query(prom, "topk(3, orders_api:db_query_p95_seconds:1m)")
    snap["slowest_queries_p95_ms"] = {
        r["metric"].get("query", "?"): round(1000 * float(r["value"][1]), 1)
        for r in slow if r["value"][1] not in ("NaN", "+Inf")}
    errors = query(prom, "topk(5, orders_api:db_errors:rate1m > 0)")
    snap["db_errors"] = {f'{r["metric"].get("kind")}:{r["metric"].get("query")}': round(float(r["value"][1]), 2)
                         for r in errors}
    snap["breaches"] = [k for k, limit in THRESHOLDS.items()
                        if snap.get(k) is not None and snap[k] > limit]
    snap["healthy"] = not snap["breaches"]
    return snap


def render(s: dict) -> str:
    fmt = lambda v, unit="": "  n/a" if v is None else f"{v:>7.2f}{unit}"
    state = "HEALTHY" if s["healthy"] else "INCIDENT: " + ", ".join(s["breaches"])
    lines = [
        f"{s['ts']}  orders-api {s['version']} ({s['git_sha']})  {state}",
        f"  requests/s     {fmt(s['rps'])}",
        f"  error rate     {fmt(s['error_rate_pct'], ' %')}",
        f"  p95 latency    {fmt(s['p95_latency_ms'], ' ms')}",
        f"  db pool in use {fmt(s['db_pool_utilization_pct'], ' %')}",
        f"  db errors/s    {fmt(s['db_errors_per_s'])}",
    ]
    if s["slowest_queries_p95_ms"]:
        lines.append("  slowest queries (p95): " + ", ".join(f"{k} {v}ms" for k, v in s["slowest_queries_p95_ms"].items()))
    if s["db_errors"]:
        lines.append("  db errors: " + ", ".join(f"{k} {v}/s" for k, v in s["db_errors"].items()))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prom", default="http://localhost:9090")
    p.add_argument("--json", action="store_true")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    while True:
        s = snapshot(args.prom)
        print(json.dumps(s) if args.json else render(s), flush=True)
        if not args.watch:
            break
        if not args.json:
            print()
        time.sleep(5)
    if args.check and not s["healthy"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
