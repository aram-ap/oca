"""JSON API under /api."""

from fastapi import APIRouter

from oca.api import actions, checks, deployments, dev, sandbox, system, tickets, tools

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(tickets.router)
api_router.include_router(actions.router)
api_router.include_router(deployments.router)
api_router.include_router(sandbox.router)
api_router.include_router(checks.router)
api_router.include_router(tools.router)
api_router.include_router(dev.router)
