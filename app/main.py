"""FastAPI application factory.

Run with::

    ./venv/bin/python -m uvicorn app.main:create_app --factory

The lifespan initialises the DB (create tables for a zero-config first run;
Alembic remains the source of truth) and disposes the engine on shutdown.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.core import logging as app_logging
from app.core.auth import RequiresLogin
from app.core.config import settings
from app.db.base import dispose_db, init_db
from app.routers import admin, auth, health, history, me, reports
from app.routers import settings as settings_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await dispose_db()


def create_app() -> FastAPI:
    app_logging.setup()
    app = FastAPI(title="Garmin → Claude", version="1.0.0", lifespan=lifespan)

    # Signed cookie sessions for web login. APP_SECRET_KEY doubles as the signing
    # key; fall back to a dev-only secret so the app still boots without it.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.APP_SECRET_KEY or "dev-insecure-session-secret",
        same_site="lax",
    )

    @app.exception_handler(RequiresLogin)
    async def _redirect_to_login(request: Request, exc: RequiresLogin):
        return RedirectResponse("/login", status_code=303)

    @app.get("/")
    async def root(request: Request):
        if request.session.get("user_id"):
            return RedirectResponse("/me", status_code=303)
        return RedirectResponse("/login", status_code=303)

    app.include_router(auth.router)
    app.include_router(settings_router.router)
    app.include_router(me.router)
    app.include_router(health.router)
    app.include_router(reports.router)
    app.include_router(history.router)
    app.include_router(admin.router)
    return app
