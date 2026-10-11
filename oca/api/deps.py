"""FastAPI dependencies that pull shared services off ``app.state``."""

from __future__ import annotations

from fastapi import Request

from oca.config import Settings
from oca.db import Database
from oca.events import EventBus


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus


def get_runner(request: Request):
    return request.app.state.runner


def get_sandbox(request: Request):
    return request.app.state.sandbox


def get_pipeline(request: Request):
    return request.app.state.pipeline


def get_scheduler(request: Request):
    return request.app.state.scheduler
