"""Server-rendered dashboard (Jinja2 + htmx). Browser only; machines use /api."""

from fastapi import APIRouter

from oca.web import auth, pages

web_router = APIRouter(include_in_schema=False)
web_router.include_router(auth.router)
web_router.include_router(pages.router)
