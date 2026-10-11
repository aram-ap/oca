"""Dry-mode helpers so the demo runs on a laptop: inject/reset the fake company."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from oca.api.deps import get_runner, get_settings
from oca.auth import Principal, require
from oca.config import Settings
from oca.schemas import Role

router = APIRouter(prefix="/dev", tags=["dev"])


class InjectIn(BaseModel):
    incident: Literal["timeout", "loyalty"] = "timeout"


def _fake(runner, settings: Settings):
    if settings.runner != "dry" or not hasattr(runner, "inject"):
        raise HTTPException(404, "Dev endpoints exist only in dry mode (use ops/inject.sh against the real stack)")
    return runner


@router.post("/inject")
async def inject(body: InjectIn, runner=Depends(get_runner), settings: Settings = Depends(get_settings),
                 _: Principal = Depends(require(Role.admin))):
    record = _fake(runner, settings).inject(body.incident)
    return {"injected": body.incident, "deployment": record.as_dict()}


@router.post("/reset")
async def reset(runner=Depends(get_runner), settings: Settings = Depends(get_settings),
                _: Principal = Depends(require(Role.admin))):
    _fake(runner, settings).reset()
    return {"reset": True}


@router.get("/state")
async def state(runner=Depends(get_runner), settings: Settings = Depends(get_settings),
                _: Principal = Depends(require(Role.admin))):
    return _fake(runner, settings).describe()
