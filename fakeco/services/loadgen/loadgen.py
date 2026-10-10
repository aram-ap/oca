#!/usr/bin/env python3
"""Simulated Driftwood customers.

Customers arrive at a steady rate (--rate visits per second, Poisson arrivals)
whether or not the site is keeping up, like real traffic. Each visit does what
the storefront page does: load the header session, the bestsellers and the
catalogue, sometimes view a product, add to cart and check out.

Continuous mode (the default, used by the loadgen container) runs forever and
prints a JSON line of stats every --report-every seconds.

Fixed mode (--requests N) stops after N requests and prints one JSON summary:
use it to measure a stack before and after a fix, e.g. in the sandbox:

    python3 services/loadgen/loadgen.py --base-url http://localhost:18080/api \\
        --requests 10000 --rate 40
"""
import argparse
import asyncio
import json
import random
import sys
import time
from collections import defaultdict

import httpx


class Stats:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.latencies: list[float] = []
        self.by_endpoint: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [requests, failures]
        self.statuses: dict[str, int] = defaultdict(int)
        self.started = time.time()

    def record(self, name: str, status: int | str, latency: float) -> None:
        ok = isinstance(status, int) and status < 500
        self.latencies.append(latency)
        self.by_endpoint[name][0] += 1
        self.by_endpoint[name][1] += 0 if ok else 1
        self.statuses[str(status)] += 1

    @property
    def count(self) -> int:
        return len(self.latencies)

    def summary(self) -> dict:
        lat = sorted(self.latencies)
        n = len(lat)
        pct = lambda p: round(lat[min(n - 1, int(p * n))] * 1000, 1) if n else None
        failures = sum(v[1] for v in self.by_endpoint.values())
        elapsed = time.time() - self.started
        return {
            "requests": n,
            "failures": failures,
            "error_rate_pct": round(100 * failures / n, 2) if n else 0.0,
            "rps": round(n / elapsed, 1) if elapsed else 0.0,
            "p50_ms": pct(0.50), "p95_ms": pct(0.95), "p99_ms": pct(0.99),
            "statuses": dict(self.statuses),
            "by_endpoint": {k: {"requests": v[0], "failures": v[1],
                                "error_rate_pct": round(100 * v[1] / v[0], 2)}
                            for k, v in sorted(self.by_endpoint.items())},
        }


async def call(client: httpx.AsyncClient, stats: Stats, name: str, method: str, path: str, **kw):
    started = time.perf_counter()
    try:
        r = await client.request(method, path, **kw)
        status: int | str = r.status_code
    except httpx.TimeoutException:
        status = "client_timeout"
    except httpx.HTTPError:
        status = "connection_error"
    stats.record(name, status, time.perf_counter() - started)


async def visit(client, stats, args):
    email = f"customer{random.randint(1, args.customers)}@example.com"
    # Page load: the storefront fires these three in parallel.
    await asyncio.gather(
        call(client, stats, "session", "GET", "/session", params={"email": email}),
        call(client, stats, "bestsellers", "GET", "/bestsellers"),
        call(client, stats, "products", "GET", "/products"),
    )
    if random.random() < 0.5:
        await call(client, stats, "product", "GET", f"/products/{random.randint(1, 14)}")
    if random.random() < args.buy_rate:
        items = [{"product_id": random.randint(1, 14), "qty": random.randint(1, 2)}
                 for _ in range(random.randint(1, 3))]
        for it in items:
            await call(client, stats, "cart", "POST", "/cart", json={"email": email, **it})
        await call(client, stats, "checkout", "POST", "/orders", json={"email": email, "items": items})


async def arrivals(client, stats, args, stop: asyncio.Event, inflight: set):
    """Start visits at Poisson-distributed intervals until stopped."""
    started = 0
    while not stop.is_set():
        if args.requests and started * 4 >= args.requests:  # ~4 requests per visit
            break
        if len(inflight) < args.max_inflight:
            task = asyncio.create_task(visit(client, stats, args))
            inflight.add(task)
            task.add_done_callback(inflight.discard)
            started += 1
        else:
            stats.record("dropped_visit", "client_overloaded", 0.0)
        await asyncio.sleep(random.expovariate(args.rate))


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://frontend/api")
    p.add_argument("--rate", type=float, default=40, help="customer visits per second (~4 requests each)")
    p.add_argument("--max-inflight", type=int, default=400, help="cap on concurrent visits")
    p.add_argument("--buy-rate", type=float, default=0.2, help="fraction of visits that check out")
    p.add_argument("--customers", type=int, default=250000, help="size of the customer email pool")
    p.add_argument("--requests", type=int, default=0, help="stop after this many requests (0 = run forever)")
    p.add_argument("--report-every", type=float, default=10.0)
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args()

    stats, window = Stats(), Stats()
    stop = asyncio.Event()

    class Tee(Stats):  # record into the run total and the current window
        def record(self, *a):
            stats.record(*a)
            window.record(*a)

    tee = Tee()
    inflight: set = set()
    limits = httpx.Limits(max_connections=300, max_keepalive_connections=300)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout, limits=limits) as client:
        producer = asyncio.create_task(arrivals(client, tee, args, stop, inflight))
        try:
            while not producer.done():
                await asyncio.wait({producer}, timeout=args.report_every)
                if not args.requests:
                    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "service": "loadgen", "rate": args.rate, "inflight": len(inflight),
                            **window.summary()}
                    line.pop("by_endpoint")
                    print(json.dumps(line), flush=True)
                    window.reset()
                else:
                    print(f"... {stats.count}/{args.requests} requests", file=sys.stderr, flush=True)
            if inflight:
                await asyncio.wait(set(inflight), timeout=args.timeout * 3)
        finally:
            producer.cancel()
            for t in list(inflight):
                t.cancel()

    if args.requests:
        print(json.dumps(stats.summary(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
