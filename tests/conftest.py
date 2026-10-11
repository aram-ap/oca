"""Shared fixtures: a dry-mode app on a temp database, an ASGI client, and auth helpers."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from oca.auth import SESSION_COOKIE, make_session
from oca.config import Settings
from oca.main import create_app, shutdown, startup
from oca.schemas import Role


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "oca.db", data_dir=tmp_path / "data", runner="dry",
                    autostart_pipeline=False, scheduler_tick_s=0.05, check_interval_s=3600, require_human_approval=True,
                    stage_delay_s=0.0, verify_runs=2, verify_interval_s=0.0, verify_settle_s=0.0, loadtest_requests=50)


@pytest.fixture
async def app(settings):
    application = create_app(settings)
    await startup(application)
    try:
        yield application
    finally:
        await shutdown(application)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://oca.test") as c:
        yield c


@pytest.fixture
def auth(settings):
    """auth(Role.engineer) -> bearer headers; auth(Role.admin) -> session cookie header."""

    def headers(role: Role) -> dict[str, str]:
        if role is Role.admin:
            return {"Cookie": f"{SESSION_COOKIE}={make_session(settings, 'admin')}"}
        return {"Authorization": f"Bearer {settings.tokens[role]}"}

    return headers
