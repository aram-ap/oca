"""Postgres access: a pooled connection wrapper that times and classifies every query."""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from .config import settings
from .telemetry import (DB_ERRORS, DB_POOL_IN_USE, DB_POOL_SIZE, DB_POOL_WAIT,
                        DB_QUERY_LATENCY, log_event)


class DatabaseTimeout(Exception):
    """A statement ran longer than DB_TIMEOUT_MS and was cancelled."""

    def __init__(self, query: str):
        super().__init__(f"database request timeout ({query})")
        self.query = query


class PoolExhausted(Exception):
    """No pooled connection became free within DB_POOL_ACQUIRE_TIMEOUT_MS."""


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None
        self._in_use = 0

    async def connect(self, retries: int = 30) -> None:
        for attempt in range(retries):
            try:
                self.pool = await asyncpg.create_pool(
                    settings.database_url,
                    min_size=settings.db_pool_min,
                    max_size=settings.db_pool_max,
                    command_timeout=settings.db_timeout_ms / 1000,
                )
                break
            except (OSError, asyncpg.PostgresError) as exc:
                log_event(logging.WARNING, "waiting for database", attempt=attempt + 1, error=str(exc))
                await asyncio.sleep(1)
        else:
            raise RuntimeError("database unavailable")
        DB_POOL_SIZE.set(settings.db_pool_max)
        log_event(logging.INFO, "database pool ready", pool_max=settings.db_pool_max,
                  db_timeout_ms=settings.db_timeout_ms,
                  pool_acquire_timeout_ms=settings.db_pool_acquire_timeout_ms)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    @asynccontextmanager
    async def connection(self):
        assert self.pool is not None
        started = time.perf_counter()
        try:
            conn = await self.pool.acquire(timeout=settings.db_pool_acquire_timeout_ms / 1000)
        except asyncio.TimeoutError:
            DB_ERRORS.labels(kind="pool_exhausted", query="acquire").inc()
            raise PoolExhausted("database connection pool exhausted")
        finally:
            DB_POOL_WAIT.observe(time.perf_counter() - started)
        self._in_use += 1
        DB_POOL_IN_USE.set(self._in_use)
        try:
            yield conn
        finally:
            self._in_use -= 1
            DB_POOL_IN_USE.set(self._in_use)
            try:
                # After a statement timeout, release() waits for Postgres to confirm the
                # cancel; under overload that can fail. Drop the connection instead of
                # letting the error replace the original one.
                await self.pool.release(conn)
            except Exception as exc:  # noqa: BLE001
                DB_ERRORS.labels(kind="release_failed", query="release").inc()
                log_event(logging.WARNING, "database connection discarded",
                          error_type=type(exc).__name__, error=str(exc)[:200])
                conn.terminate()

    async def run(self, conn: asyncpg.Connection, name: str, method: str, sql: str, *args):
        """Run one named statement. method is fetch, fetchrow, fetchval or execute."""
        started = time.perf_counter()
        try:
            return await getattr(conn, method)(sql, *args)
        except asyncio.TimeoutError:
            DB_ERRORS.labels(kind="timeout", query=name).inc()
            raise DatabaseTimeout(name)
        except asyncpg.PostgresError:
            DB_ERRORS.labels(kind="postgres_error", query=name).inc()
            raise
        finally:
            DB_QUERY_LATENCY.labels(query=name).observe(time.perf_counter() - started)

    async def migrate(self) -> None:
        """Apply any SQL files in migrations/ that have not run yet, in name order."""
        directory = Path(settings.migrations_dir)
        async with self.connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            applied = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in applied:
                    continue
                log_event(logging.INFO, "applying migration", migration=path.name)
                # Migrations may build indexes on large tables; don't apply the request timeout.
                await conn.execute(path.read_text(), timeout=None)
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)


db = Database()
