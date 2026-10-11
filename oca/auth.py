"""Bearer tokens for machine clients, a signed session cookie for the dashboard, role checks."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from oca.config import Settings
from oca.schemas import Actor, Role

SESSION_COOKIE = "oca_session"
SESSION_MAX_AGE_S = 12 * 3600

ROLE_ACTOR: dict[Role, Actor] = {
    Role.investigator: Actor.investigator, Role.engineer: Actor.engineer, Role.guardian: Actor.guardian,
    Role.orchestrator: Actor.orchestrator, Role.health: Actor.health_check, Role.jira: Actor.jira,
    Role.slack: Actor.slack, Role.admin: Actor.human,
}


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role

    @property
    def actor(self) -> Actor:
        return ROLE_ACTOR[self.role]

    def has(self, *roles: Role) -> bool:
        return self.role is Role.admin or self.role in roles


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="oca-session")


def make_session(settings: Settings, user: str) -> str:
    return _serializer(settings).dumps({"u": user})


def read_session(settings: Settings, cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        return _serializer(settings).loads(cookie, max_age=SESSION_MAX_AGE_S)["u"]
    except (BadSignature, SignatureExpired, KeyError, TypeError):
        return None


def bearer_principal(request: Request, settings: Settings) -> Principal | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    role = settings.role_for_token(header[7:].strip())
    return Principal(name=role.value, role=role) if role else None


def session_principal(request: Request, settings: Settings) -> Principal | None:
    user = read_session(settings, request.cookies.get(SESSION_COOKIE))
    return Principal(name=user, role=Role.admin) if user else None


def optional_principal(request: Request, settings: Settings = Depends(get_settings)) -> Principal | None:
    return bearer_principal(request, settings) or session_principal(request, settings)


def current_principal(principal: Principal | None = Depends(optional_principal)) -> Principal:
    if principal is None:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    return principal


def require(*roles: Role):
    """Dependency factory: the caller must hold one of ``roles`` (admin always passes)."""

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has(*roles):
            raise HTTPException(403, f"Role {principal.role.value!r} may not call this endpoint")
        return principal

    return dependency
