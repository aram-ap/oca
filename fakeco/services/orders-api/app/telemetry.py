"""Structured JSON logging and Prometheus metrics."""
import json
import logging
import sys
import time

from prometheus_client import Counter, Gauge, Histogram

from .config import settings

# ---------------------------------------------------------------- metrics

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.18, 0.25, 0.5, 1, 2, 3, 5, 10)

APP_INFO = Gauge("app_info", "Build info for the running orders-api", ["version", "git_sha"])

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route"],
    buckets=LATENCY_BUCKETS)

DB_QUERY_LATENCY = Histogram(
    "db_query_duration_seconds", "SQL statement latency by named query", ["query"],
    buckets=LATENCY_BUCKETS)
DB_ERRORS = Counter(
    "db_errors_total", "Database errors by kind", ["kind", "query"])
DB_POOL_SIZE = Gauge("db_pool_size", "Maximum connections in the pool")
DB_POOL_IN_USE = Gauge("db_pool_in_use", "Connections currently checked out")
DB_POOL_WAIT = Histogram(
    "db_pool_acquire_wait_seconds", "Time spent waiting for a pooled connection",
    buckets=LATENCY_BUCKETS)

CACHE_REQUESTS = Counter("cache_requests_total", "Redis cache lookups", ["key", "result"])
ORDERS_PLACED = Counter("orders_placed_total", "Orders successfully placed")
ORDER_REVENUE = Counter("order_revenue_cents_total", "Revenue from placed orders, in cents")

# ---------------------------------------------------------------- logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": "orders-api",
            "version": settings.version,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info and record.levelno >= logging.ERROR:
            entry["exception"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(entry)


def setup_logging() -> logging.Logger:
    log = logging.getLogger("orders-api")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    formatter = JsonFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_file:
        handlers.append(logging.FileHandler(settings.log_file))
    for h in handlers:
        h.setFormatter(formatter)
        log.addHandler(h)
    log.propagate = False
    return log


log = setup_logging()


def log_event(level: int, msg: str, **fields) -> None:
    log.log(level, msg, extra={"fields": fields})
