"""Standalone, bounded, read-only request-abuse exercise against Driftwood.

Run on a separate LAN machine. JSONL output separates traffic from customer probes.
No deployment, config mutation, or OCA ticket creation is performed here.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import http.client
import ipaddress
import json
import threading
import time
from urllib.parse import urlsplit


def local_target(value: str):
    target = urlsplit(value)
    host = target.hostname
    allowed = host == "localhost"
    if host:
        try:
            address = ipaddress.ip_address(host)
            allowed = address.is_loopback or any(address in network for network in (
                ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7")))
        except ValueError:
            pass
    if (target.scheme != "http" or not allowed
            or target.username or target.password or target.query or target.fragment):
        raise argparse.ArgumentTypeError("use an http URL with the GX10's literal private LAN IP or loopback address")
    try:
        target.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return target


def request(target, path: str, timeout: float):
    started = time.monotonic()
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=timeout)
    try:
        connection.request("GET", target.path.rstrip("/") + path,
                           headers={"User-Agent": "driftwood-demo-client"})
        response = connection.getresponse()
        response.read(65536)
        return {"status": response.status, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except (OSError, http.client.HTTPException) as exc:
        return {"status": 0, "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "error": type(exc).__name__}
    finally:
        connection.close()


def run(args):
    stop = threading.Event()
    lock = threading.Lock()
    started = time.monotonic()
    deadline = started + args.baseline_seconds + args.seconds
    counts = {}

    def emit(kind, **fields):
        with lock:
            print(json.dumps({"kind": kind, "elapsed_s": round(time.monotonic() - started, 2),
                              **fields}), flush=True)

    def traffic():
        while not stop.is_set() and time.monotonic() < deadline:
            tick = time.monotonic()
            result = request(args.target, args.attack_path, args.timeout)
            with lock:
                code = str(result["status"])
                counts[code] = counts.get(code, 0) + 1
            stop.wait(max(0, args.concurrency / args.rps - (time.monotonic() - tick)))

    def probes():
        while not stop.is_set() and time.monotonic() < deadline:
            emit("customer_probe", phase="baseline" if time.monotonic() < started + args.baseline_seconds else ("observation" if args.probe_only else "abuse"),
                 **request(args.target, args.probe_path, args.timeout))
            stop.wait(args.probe_interval)

    emit("start", target=args.target.geturl(), attack_path=args.attack_path,
         probe_path=args.probe_path, concurrency=args.concurrency, maximum_rps=args.rps)
    with ThreadPoolExecutor(max_workers=args.concurrency + 1) as pool:
        futures = [pool.submit(probes)]
        try:
            if not stop.wait(args.baseline_seconds):
                emit("observation_started" if args.probe_only else "abuse_started")
                if not args.probe_only:
                    futures.extend(pool.submit(traffic) for _ in range(args.concurrency))
                stop.wait(max(0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            emit("interrupted")
        finally:
            stop.set()
        for future in futures:
            future.result()
    emit("finished", attack_statuses=counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=local_target, required=True,
                        help="FakeCo frontend, e.g. http://192.168.1.50:8080/api")
    parser.add_argument("--probe-only", action="store_true", help="observe customers without sending abuse")
    parser.add_argument("--attack-path", default="/products/1")
    parser.add_argument("--probe-path", default="/products/2")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--baseline-seconds", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--rps", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--probe-interval", type=float, default=2)
    args = parser.parse_args()
    if not (1 <= args.seconds <= 1800 and 0 <= args.baseline_seconds <= 120
            and 1 <= args.concurrency <= 32 and 1 <= args.rps <= 100
            and 0.1 <= args.timeout <= 10 and 0.5 <= args.probe_interval <= 30):
        parser.error("bounds: duration 1–1800s, baseline 0–120s, concurrency 1–32, rps 1–100, timeout .1–10s, probe .5–30s")
    for path in (args.attack_path, args.probe_path):
        if not path.startswith("/") or path.startswith("//") or "#" in path or "\r" in path or "\n" in path:
            parser.error("paths must start with one slash and contain no fragment or newline")
    run(args)


if __name__ == "__main__":
    main()
