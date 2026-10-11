"""Agent tool endpoints. Request shapes match models/tools/catalog.py exactly; access is per role."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from oca.api.deps import get_db, get_runner
from oca.auth import Principal, require
from oca.db import Database
from oca.fakeco import diffcheck
from oca.fakeco.runner import RunnerError
from oca.schemas import Role

router = APIRouter(prefix="/tools", tags=["tools"])
Service = Literal["orders-api", "frontend", "postgres", "redis"]
# Keep every tool response comfortably inside the agents' observation budget.
MAX_CODE_CHARS = 16_000
MAX_DIFF_CHARS = 14_000
LOG_ENTRY_BUDGET = 12_000
LOG_FIELDS = ("ts", "level", "msg", "method", "route", "path", "status", "duration_ms", "query", "db_timeout_ms",
              "pool_max", "acquire_timeout_ms", "error_type", "error", "upstream_ms", "request_id")
CONTAINER_FIELDS = ("Name", "Service", "Image", "State", "Status", "Health", "Ports", "RunningFor", "RestartCount")


def compact_log(entry: dict) -> dict:
    out = {k: entry[k] for k in LOG_FIELDS if k in entry and entry[k] not in (None, "")}
    if isinstance(out.get("request_id"), str):
        out["request_id"] = out["request_id"][:8]
    if not out:
        out = {"msg": str(entry)[:200]}
    return out


def log_summary(rows: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    messages: dict[str, int] = {}
    for r in rows:
        status = r.get("status")
        if isinstance(status, int):
            by_status[str(status)] = by_status.get(str(status), 0) + 1
            if status >= 500:
                key = f"{r.get('msg', 'error')} route={r.get('route') or r.get('path')} query={r.get('query')}"
                messages[key] = messages.get(key, 0) + 1
    top = sorted(messages.items(), key=lambda kv: -kv[1])[:5]
    return {"by_status": by_status, "top_errors": [{"message": m, "count": c} for m, c in top]}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@router.get("/logs")
async def get_logs(service: Service, lines: int = Query(100, ge=1, le=200), runner=Depends(get_runner),
                   _: Principal = Depends(require(Role.investigator))):
    try:
        rows = await runner.read_logs(service, lines)
    except RunnerError as exc:
        raise HTTPException(502, str(exc))
    compact = [compact_log(r) for r in rows]
    errors = sum(1 for r in compact if isinstance(r.get("status"), int) and r["status"] >= 500)
    # Error lines first (newest first), then the rest, until the byte budget is spent.
    ordered = sorted(compact, key=lambda r: (0 if isinstance(r.get("status"), int) and r["status"] >= 500 else 1))
    entries, used = [], 0
    for r in ordered:
        size = len(str(r)) + 2
        if used + size > LOG_ENTRY_BUDGET:
            break
        entries.append(r)
        used += size
    return {"evidence_ref": f"logs:{service}:{_stamp()}", "service": service, "lines": len(rows), "error_lines": errors,
            "returned": len(entries), "truncated": len(entries) < len(compact),
            "note": "entries are compacted; error lines listed first" if len(entries) < len(compact) else None,
            "summary": log_summary(compact), "entries": entries}


@router.get("/containers")
async def get_containers(runner=Depends(get_runner), _: Principal = Depends(require(Role.investigator))):
    try:
        rows = await runner.containers()
    except RunnerError as exc:
        raise HTTPException(502, str(exc))
    compact = [{k: r.get(k) for k in CONTAINER_FIELDS if r.get(k) not in (None, "")} for r in rows]
    return {"evidence_ref": f"containers:{_stamp()}", "containers": compact}


@router.get("/metrics")
async def get_metrics(service: Service, runner=Depends(get_runner), db: Database = Depends(get_db),
                      _: Principal = Depends(require(Role.investigator))):
    try:
        series = await runner.metrics(service)
        snap = await runner.status()
    except (RunnerError, Exception) as exc:  # noqa: BLE001 - Prometheus may be unreachable
        raise HTTPException(502, f"metrics unavailable: {exc}")
    return {"evidence_ref": f"metrics:{service}:{_stamp()}", "service": service, "series": series,
            "status": snap.as_dict(), "baseline": await db.get_setting("baseline", {}),
            "thresholds": await db.get_setting("thresholds", {})}


@router.get("/code")
async def get_code(path: str = Query(..., min_length=1), runner=Depends(get_runner),
                   _: Principal = Depends(require(Role.investigator, Role.engineer))):
    try:
        content = await runner.read_file(path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except FileNotFoundError:
        raise HTTPException(404, f"No such file: {path}")
    truncated = len(content) > MAX_CODE_CHARS
    return {"evidence_ref": f"code:{path}", "path": path, "content": content[:MAX_CODE_CHARS], "truncated": truncated,
            "lines": content.count("\n") + 1}


@router.get("/git")
async def get_git(operation: Literal["history", "diff"], ref: str | None = Query(None, min_length=1, max_length=120),
                  runner=Depends(get_runner), _: Principal = Depends(require(Role.investigator, Role.engineer))):
    try:
        deployments = await runner.deployments(limit=20)
        if operation == "history":
            commits = await runner.git_log("main", 20)
            return {"evidence_ref": f"git:history:{_stamp()}", "deployments": [d.as_dict() for d in deployments],
                    "commits": [c.as_dict() for c in commits]}
        target = None
        if ref is None and deployments:
            target = deployments[0]
        elif ref is not None:
            for d in deployments:
                if ref == str(d.deploy_id) or ref == d.git_sha:
                    target = d
                    break
        if target is not None:
            if not target.prev_sha:
                raise HTTPException(404, "First deployment has no predecessor to diff against")
            diff = await runner.git_diff(target.prev_sha, target.git_sha)
            label = f"deploy {target.deploy_id} ({target.prev_sha}..{target.git_sha})"
        elif ref and ".." in ref:
            a, _, b = ref.partition("..")
            diff = await runner.git_diff(a, b.lstrip("."))
            label = ref
        elif ref and ref.startswith("fix/"):
            diff = await runner.sandbox_diff(ref)
            label = ref
        else:
            raise HTTPException(404, f"Unknown ref {ref!r}; use a deploy id, sha, a..b, or fix/<ticket>")
    except RunnerError as exc:
        raise HTTPException(502, str(exc))
    info = diffcheck.parse(diff)
    return {"evidence_ref": f"git:diff:{label}", "ref": label, "diff": diff[:MAX_DIFF_CHARS],
            "truncated": len(diff) > MAX_DIFF_CHARS, "files_changed": info.files,
            "lines_added": info.added, "lines_removed": info.removed}


# ------------------------------------------------------------------ sandbox (engineer)

from pydantic import BaseModel, Field  # noqa: E402

from oca.api.deps import get_sandbox  # noqa: E402
from oca.sandbox import SandboxError  # noqa: E402


class PatchIn(BaseModel):
    ticket_id: str = Field(min_length=1)
    diff: str = Field(min_length=1)


class TicketIn(BaseModel):
    ticket_id: str = Field(min_length=1)


class LoadtestIn(TicketIn):
    requests: int | None = Field(None, ge=1, le=10000)


def _sandbox_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SandboxError):
        return HTTPException(409, str(exc))
    return HTTPException(502, f"sandbox operation failed: {exc}")


@router.post("/sandbox/patch")
async def sandbox_patch(body: PatchIn, sandbox=Depends(get_sandbox), _: Principal = Depends(require(Role.engineer))):
    try:
        return await sandbox.apply_patch(body.ticket_id, body.diff)
    except (SandboxError, RunnerError) as exc:
        raise _sandbox_error(exc)


@router.post("/sandbox/test")
async def sandbox_test(body: TicketIn, sandbox=Depends(get_sandbox), _: Principal = Depends(require(Role.engineer))):
    try:
        return await sandbox.run_tests(body.ticket_id)
    except (SandboxError, RunnerError) as exc:
        raise _sandbox_error(exc)


@router.post("/sandbox/loadtest")
async def sandbox_loadtest(body: LoadtestIn, sandbox=Depends(get_sandbox), _: Principal = Depends(require(Role.engineer))):
    try:
        return await sandbox.run_loadtest(body.ticket_id, body.requests)
    except (SandboxError, RunnerError) as exc:
        raise _sandbox_error(exc)


# ------------------------------------------------------------------- review (guardian)


@router.get("/diff")
async def review_diff(ticket_id: str = Query(..., min_length=1), sandbox=Depends(get_sandbox),
                      _: Principal = Depends(require(Role.guardian))):
    try:
        return await sandbox.diff(ticket_id)
    except (SandboxError, RunnerError) as exc:
        raise _sandbox_error(exc)


@router.get("/test-results")
async def review_tests(ticket_id: str = Query(..., min_length=1), sandbox=Depends(get_sandbox),
                       _: Principal = Depends(require(Role.guardian))):
    try:
        return await sandbox.test_results(ticket_id)
    except SandboxError as exc:
        raise _sandbox_error(exc)


@router.get("/sandbox-results")
async def review_sandbox(ticket_id: str = Query(..., min_length=1), sandbox=Depends(get_sandbox),
                         _: Principal = Depends(require(Role.guardian))):
    try:
        return await sandbox.results(ticket_id)
    except SandboxError as exc:
        raise _sandbox_error(exc)
