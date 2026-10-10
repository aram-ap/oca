"""Driftwood Coffee Co. orders API.

Serves the product catalogue, bestseller list, customer session and checkout
for the storefront. Postgres is the system of record; Redis holds carts and
short-lived caches.
"""
import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from .config import settings
from .db import DatabaseTimeout, PoolExhausted, db
from .telemetry import (APP_INFO, CACHE_REQUESTS, HTTP_LATENCY, HTTP_REQUESTS,
                        ORDER_REVENUE, ORDERS_PLACED, log_event)

cache: aioredis.Redis | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cache
    APP_INFO.labels(version=settings.version, git_sha=settings.git_sha).set(1)
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, ctx: log_event(logging.WARNING, "background task error",
                                    error=str(ctx.get("exception") or ctx.get("message"))[:200]))
    await db.connect()
    await db.migrate()
    cache = aioredis.Redis(connection_pool=aioredis.BlockingConnectionPool.from_url(
        settings.redis_url, decode_responses=True, max_connections=100, timeout=2))
    log_event(logging.INFO, "orders-api started", git_sha=settings.git_sha)
    yield
    await db.close()
    await cache.aclose()


app = FastAPI(title="Driftwood orders API", lifespan=lifespan)


# ---------------------------------------------------------------- middleware

@app.middleware("http")
async def observe(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    started = time.perf_counter()
    status = 500
    try:
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - see on_unhandled
            response = await on_unhandled(request, exc)
        status = response.status_code
        response.headers["x-request-id"] = request_id
        return response
    finally:
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        if path not in ("/metrics",):
            HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
            HTTP_LATENCY.labels(request.method, path).observe(elapsed)
            level = logging.ERROR if status >= 500 else logging.INFO
            log_event(level, "request", request_id=request_id, method=request.method,
                      route=path, status=status, duration_ms=round(elapsed * 1000, 1))


@app.exception_handler(DatabaseTimeout)
async def on_db_timeout(request: Request, exc: DatabaseTimeout):
    log_event(logging.ERROR, "database request timeout", request_id=request.state.request_id,
              query=exc.query, db_timeout_ms=settings.db_timeout_ms)
    return JSONResponse({"error": "database request timeout"}, status_code=500)


@app.exception_handler(PoolExhausted)
async def on_pool_exhausted(request: Request, exc: PoolExhausted):
    log_event(logging.ERROR, "database connection pool exhausted",
              request_id=request.state.request_id, pool_max=settings.db_pool_max,
              acquire_timeout_ms=settings.db_pool_acquire_timeout_ms)
    return JSONResponse({"error": "service busy"}, status_code=503)


async def on_unhandled(request: Request, exc: Exception):
    """Everything else: keep the log line structured so it can be searched and counted."""
    if isinstance(exc, aioredis.RedisError):
        msg, kind = "cache (redis) error", type(exc).__name__
    elif isinstance(exc, TimeoutError):
        msg, kind = "database request timeout", "timeout"
    elif isinstance(exc, (asyncpg.PostgresError, asyncpg.InterfaceError, ConnectionError, OSError)):
        msg, kind = "database connection error", type(exc).__name__
    else:
        msg, kind = "unhandled exception", type(exc).__name__
    log_event(logging.ERROR, msg, request_id=getattr(request.state, "request_id", None),
              error_type=kind, error=str(exc)[:300])
    return JSONResponse({"error": "internal error"}, status_code=500)


# ---------------------------------------------------------------- helpers

_refresh_locks: dict[str, asyncio.Lock] = {}


async def cached(key: str, ttl: int, loader):
    """Read-through Redis cache. On a miss only one request per key runs the loader;
    concurrent requests wait for it instead of stampeding the database."""
    raw = await cache.get(key)
    if raw is not None:
        CACHE_REQUESTS.labels(key=key, result="hit").inc()
        return json.loads(raw)
    async with _refresh_locks.setdefault(key, asyncio.Lock()):
        raw = await cache.get(key)  # filled while we waited?
        if raw is not None:
            CACHE_REQUESTS.labels(key=key, result="hit").inc()
            return json.loads(raw)
        CACHE_REQUESTS.labels(key=key, result="miss").inc()
        value = await loader()
        await cache.set(key, json.dumps(value), ex=ttl)
        return value


def cart_key(email: str) -> str:
    return f"cart:{email.lower()}"


# ---------------------------------------------------------------- ops endpoints

@app.get("/health")
async def health():
    checks = {}
    try:
        async with db.connection() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"error: {exc}"
    try:
        await cache.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"
    healthy = all(v == "ok" for v in checks.values())
    body = {"status": "ok" if healthy else "degraded", "version": settings.version,
            "git_sha": settings.git_sha, "checks": checks}
    return JSONResponse(body, status_code=200 if healthy else 503)


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------- catalogue

@app.get("/products")
async def list_products():
    async def load():
        async with db.connection() as conn:
            rows = await db.run(conn, "list_products", "fetch",
                                "SELECT id, sku, name, origin, roast, price_cents FROM products ORDER BY id")
        return [dict(r) for r in rows]
    return await cached("products", settings.products_cache_ttl_s, load)


@app.get("/products/{product_id}")
async def get_product(product_id: int):
    async with db.connection() as conn:
        row = await db.run(conn, "get_product", "fetchrow",
                           "SELECT id, sku, name, origin, roast, price_cents, description, stock "
                           "FROM products WHERE id = $1", product_id)
    if row is None:
        raise HTTPException(404, "product not found")
    return dict(row)


@app.get("/bestsellers")
async def bestsellers():
    """Top products by units sold over the last 30 days, for the storefront homepage."""
    async def load():
        async with db.connection() as conn:
            rows = await db.run(
                conn, "bestsellers_30d", "fetch",
                """
                SELECT p.id, p.name, p.origin, sum(oi.qty) AS units
                FROM order_items oi
                JOIN orders o   ON o.id = oi.order_id
                JOIN products p ON p.id = oi.product_id
                WHERE o.created_at > now() - interval '30 days'
                GROUP BY p.id, p.name, p.origin
                ORDER BY units DESC
                LIMIT 5
                """)
        return [dict(r) for r in rows]
    return await cached("bestsellers", settings.bestsellers_cache_ttl_s, load)


# ---------------------------------------------------------------- session & cart

@app.get("/session")
async def session(email: str):
    """Called on every storefront page view to render the header (greeting, cart badge)."""
    items = await cache.hgetall(cart_key(email))
    return {"email": email, "cart_items": sum(int(q) for q in items.values())}


class CartItem(BaseModel):
    email: str
    product_id: int
    qty: int = Field(1, ge=1, le=20)


@app.post("/cart")
async def add_to_cart(item: CartItem):
    key = cart_key(item.email)
    await cache.hincrby(key, str(item.product_id), item.qty)
    await cache.expire(key, 3600)
    return {"ok": True}


# ---------------------------------------------------------------- checkout

class OrderLine(BaseModel):
    product_id: int
    qty: int = Field(ge=1, le=20)


class OrderIn(BaseModel):
    email: str
    items: list[OrderLine] = Field(min_length=1, max_length=10)


@app.post("/orders", status_code=201)
async def place_order(order: OrderIn):
    product_ids = sorted({line.product_id for line in order.items})
    async with db.connection() as conn:
        async with conn.transaction():
            rows = await db.run(conn, "lock_products", "fetch",
                                "SELECT id, price_cents, stock FROM products "
                                "WHERE id = ANY($1::int[]) ORDER BY id FOR UPDATE", product_ids)
            products = {r["id"]: r for r in rows}
            missing = [pid for pid in product_ids if pid not in products]
            if missing:
                raise HTTPException(400, f"unknown products: {missing}")
            for line in order.items:
                if products[line.product_id]["stock"] < line.qty:
                    raise HTTPException(409, f"product {line.product_id} out of stock")

            total = sum(products[l.product_id]["price_cents"] * l.qty for l in order.items)
            order_id = await db.run(conn, "insert_order", "fetchval",
                                    "INSERT INTO orders (customer_email, total_cents, status) "
                                    "VALUES ($1, $2, 'paid') RETURNING id",
                                    order.email.lower(), total)
            await db.run(conn, "insert_order_items", "executemany",
                         "INSERT INTO order_items (order_id, product_id, qty, price_cents) "
                         "VALUES ($1, $2, $3, $4)",
                         [(order_id, l.product_id, l.qty, products[l.product_id]["price_cents"])
                          for l in order.items])
            await db.run(conn, "decrement_stock", "executemany",
                         "UPDATE products SET stock = stock - $2 WHERE id = $1",
                         [(l.product_id, l.qty) for l in order.items])

    await cache.delete(cart_key(order.email))
    ORDERS_PLACED.inc()
    ORDER_REVENUE.inc(total)
    return {"order_id": order_id, "total_cents": total, "status": "paid"}


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    async with db.connection() as conn:
        order = await db.run(conn, "get_order", "fetchrow",
                             "SELECT id, customer_email, total_cents, status, created_at "
                             "FROM orders WHERE id = $1", order_id)
        if order is None:
            raise HTTPException(404, "order not found")
        items = await db.run(conn, "get_order_items", "fetch",
                             "SELECT product_id, qty, price_cents FROM order_items WHERE order_id = $1",
                             order_id)
    return {**dict(order), "items": [dict(i) for i in items]}
