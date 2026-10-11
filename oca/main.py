"""FastAPI application factory and process entry point (``python -m oca.main``)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from oca.api import api_router
from oca.backup import BackupManager
from oca.checks.scheduler import start_scheduler
from oca.executor import Executor
from oca.gate import load_allowlist
from oca.model import set_model
from oca.reports import start_reports
from oca.integrations import start_integrations, start_outbox
from oca.integrations.jira import JIRA_DEFAULTS
from oca.integrations.slack import SLACK_DEFAULTS
from oca.pipeline import Pipeline
from oca.sandbox import SandboxManager
from oca.web import web_router
from oca.tz import set_timezone
from oca.config import Settings
from oca.db import Database
from oca.events import EventBus

log = logging.getLogger("oca")

DEFAULT_SETTINGS = {
    "require_human_approval": False,
    "risk_threshold": 30,
    "baseline": {"error_rate_pct": 0.0, "p95_latency_ms": 20.0},
    "thresholds": {"error_rate_pct": 5.0, "p95_latency_ms": 1000.0, "db_pool_utilization_pct": 90.0,
                   "metrics_factor": 5.0},
    "blocklist": [],
    "cooldown_s": 120,
    "reopen_cooldown_s": 120,
    "timezone": "UTC",
    "jira": dict(JIRA_DEFAULTS),
    "slack": dict(SLACK_DEFAULTS),
}


async def startup(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    await app.state.db.connect()
    seeds = dict(DEFAULT_SETTINGS, require_human_approval=settings.require_human_approval,
                 risk_threshold=settings.risk_threshold, timezone=settings.timezone, model=settings.model)
    for key, value in seeds.items():
        await app.state.db.seed_setting(key, value)
    set_timezone(app, await app.state.db.get_setting("timezone", settings.timezone))
    try:
        set_model(app, await app.state.db.get_setting("model", settings.model))
    except ValueError:
        set_model(app, settings.model)
    app.state.background = []
    for starter in app.state.service_starters:
        task = await starter(app)
        if task is not None:
            app.state.background.append(task)


async def shutdown(app: FastAPI) -> None:
    for owner in ("executor", "pipeline"):
        service = getattr(app.state, owner, None)
        if service is not None:
            for task in list(service.tasks.values()):
                if not task.done():
                    task.cancel()
    for task in getattr(app.state, "background", []):
        task.cancel()
    for task in getattr(app.state, "background", []):
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
            pass
    await app.state.db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup(app)
    try:
        yield
    finally:
        await shutdown(app)


def build_runner(settings: Settings):
    if settings.runner == "real":
        from oca.fakeco.subprocess_runner import SubprocessRunner

        return SubprocessRunner(settings.fakeco_root, settings.prom_url, settings.sandbox_prom_url,
                                settings.sandbox_api_url, settings.service_urls,
                                data_dir=settings.data_dir, sandbox_profile=settings.sandbox_profile)
    from oca.fakeco.fake_runner import FakeRunner

    return FakeRunner()


def create_app(settings: Settings | None = None, runner=None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="OCA — On-Call Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.bus = EventBus()
    app.state.db = Database(settings.db_path, app.state.bus)
    app.state.runner = runner if runner is not None else build_runner(settings)
    app.state.allowlist = load_allowlist(settings.allowlist_path)
    app.state.sandbox = SandboxManager(app)
    app.state.backup = BackupManager(app)
    app.state.executor = Executor(app)
    app.state.pipeline = Pipeline(app)
    app.state.scheduler = None
    # Later phases append coroutines ``async def start(app) -> asyncio.Task | None`` here.
    app.state.service_starters = [start_integrations, start_scheduler, start_outbox, start_reports]
    app.include_router(api_router, prefix="/api")
    app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")
    app.include_router(web_router)
    return app


GRACEFUL_SHUTDOWN_S = 5


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    # Dashboard SSE streams stay open indefinitely; without a graceful-shutdown timeout uvicorn would wait
    # for them forever after SIGTERM, leaving a process that no longer listens but keeps its scheduler running.
    app = create_app(settings)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info",
                            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S)
    app.state.server = uvicorn.Server(config)  # the dashboard's Shut down / Restart buttons flip should_exit
    app.state.server.run()
    if getattr(app.state, "restart_requested", False):
        restart_process()


def restart_process() -> None:
    """Replace this process with a fresh ``python -m oca.main`` (same cwd, env and arguments), so the
    dashboard's Restart button picks up pulled code without anyone at a terminal."""
    argv = [sys.executable, "-m", "oca.main", *sys.argv[1:]]
    logging.getLogger("oca.main").warning("restarting: %s", " ".join(argv))
    os.execv(sys.executable, argv)


if __name__ == "__main__":
    main()
