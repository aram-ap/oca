from __future__ import annotations

import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from oca.auth import SESSION_COOKIE, SESSION_MAX_AGE_S, make_session, session_principal
from oca.web.common import render

router = APIRouter()


@router.get("/")
async def root(request: Request):
    target = "/dash" if session_principal(request, request.app.state.settings) else "/login"
    return RedirectResponse(target, status_code=303)


@router.get("/login")
async def login_form(request: Request):
    return render(request, "login.html", error=None)


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    settings = request.app.state.settings
    if hmac.compare_digest(username, settings.admin_user) and hmac.compare_digest(password, settings.admin_password):
        response = RedirectResponse("/dash", status_code=303)
        response.set_cookie(SESSION_COOKIE, make_session(settings, username), max_age=SESSION_MAX_AGE_S,
                            httponly=True, samesite="lax")
        return response
    return render(request, "login.html", error="Invalid credentials")


@router.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
