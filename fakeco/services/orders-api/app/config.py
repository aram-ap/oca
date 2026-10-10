"""Runtime configuration for the orders API.

Every value comes from the environment. In Docker the environment is loaded
from services/orders-api/config/app.env, which is checked into git, so every
config change is a commit and shows up in the deploy history.
"""
import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql://driftwood:driftwood@localhost:5432/driftwood"))
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))

    # How long a single SQL statement may run before it is cancelled.
    db_timeout_ms: int = field(default_factory=lambda: _int("DB_TIMEOUT_MS", 3000))
    db_pool_min: int = field(default_factory=lambda: _int("DB_POOL_MIN", 2))
    db_pool_max: int = field(default_factory=lambda: _int("DB_POOL_MAX", 10))
    # How long a request waits for a free pooled connection.
    db_pool_acquire_timeout_ms: int = field(default_factory=lambda: _int("DB_POOL_ACQUIRE_TIMEOUT_MS", 2000))

    products_cache_ttl_s: int = field(default_factory=lambda: _int("PRODUCTS_CACHE_TTL_S", 60))
    bestsellers_cache_ttl_s: int = field(default_factory=lambda: _int("BESTSELLERS_CACHE_TTL_S", 30))

    version: str = field(default_factory=lambda: os.getenv("APP_VERSION", "dev"))
    git_sha: str = field(default_factory=lambda: os.getenv("GIT_SHA", "unknown"))
    log_file: str | None = field(default_factory=lambda: os.getenv("LOG_FILE") or None)
    migrations_dir: str = field(default_factory=lambda: os.getenv("MIGRATIONS_DIR", "migrations"))


settings = Settings()
