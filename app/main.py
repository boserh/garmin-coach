"""FastAPI application factory.

Run with::

    ./venv/bin/python -m uvicorn app.main:create_app --factory

The lifespan initialises the DB (create tables for a zero-config first run;
Alembic remains the source of truth) and disposes the engine on shutdown.
"""
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core import logging as app_logging
from app.core.auth import RequiresLogin
from app.core.config import settings
from app.db.base import dispose_db, init_db
from app.garmin.mfa import MFARequired
from app.routers import admin, auth, chat, dashboard, health, history, me, plan, reports
from app.routers import settings as settings_router

logger = logging.getLogger("api")


class _RevalidatingStatic(StaticFiles):
    """Serve static assets with ``Cache-Control: no-cache`` so the browser revalidates
    (via the ETag StaticFiles already sends) on every load — a cheap 304 when unchanged,
    a fresh 200 right after a deploy. Without this a changed app.css could sit stale in a
    browser's heuristic cache long after a deploy (the classic "CSS didn't update" bug).
    Combined with the ``?v=`` link bump that one-time-breaks any already-cached copy."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await dispose_db()


def create_app() -> FastAPI:
    app_logging.setup()
    app = FastAPI(title="Garmin → Claude", version="1.0.0", lifespan=lifespan)

    # Signed cookie sessions for web login. APP_SECRET_KEY doubles as the signing
    # key. If it's unset we must NOT fall back to a constant — a known secret lets
    # anyone forge a session cookie with an admin user_id (SEC-01). Instead we log
    # loudly and sign with an ephemeral per-process key: sessions won't survive a
    # restart, but nobody can forge one. (Credential encryption still needs a real
    # APP_SECRET_KEY — app.core.crypto fails without it, so that stays a hard error.)
    session_secret = settings.APP_SECRET_KEY
    if not session_secret:
        logger.error(
            "AUTH: APP_SECRET_KEY is not set — sessions are signed with an ephemeral "
            "per-process key (they won't survive a restart). Set APP_SECRET_KEY in .env "
            "for stable, non-forgeable sessions."
        )
        session_secret = Fernet.generate_key().decode()
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
    )

    @app.middleware("http")
    async def _log_requests(request: Request, call_next):
        # Per-request access log in the app's format (uvicorn.access stays quiet).
        # /health is polled by uptime checks — skip it to avoid noise.
        start = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        if path == "/health":
            return response
        ms = (time.perf_counter() - start) * 1000
        logger.info(f"{request.method} {path} → {response.status_code} {ms:.0f}ms")
        return response

    app.mount(
        "/static",
        _RevalidatingStatic(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )

    @app.exception_handler(RequiresLogin)
    async def _redirect_to_login(request: Request, exc: RequiresLogin):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(MFARequired)
    async def _mfa_required(request: Request, exc: MFARequired):
        return JSONResponse(
            {
                "error": "garmin_mfa_required",
                "message": "Garmin просить код підтвердження — заверши вхід у Налаштуваннях.",
                "settings_url": "/settings",
            },
            status_code=409,
        )

    @app.get("/")
    async def root(request: Request):
        if request.session.get("user_id"):
            # EP-04: the dashboard is the product home (was /me).
            return RedirectResponse("/dashboard", status_code=303)
        return RedirectResponse("/login", status_code=303)

    app.include_router(auth.router)
    app.include_router(settings_router.router)
    app.include_router(dashboard.router)
    app.include_router(me.router)
    app.include_router(plan.router)
    app.include_router(chat.router)
    app.include_router(health.router)
    app.include_router(reports.router)
    app.include_router(history.router)
    app.include_router(admin.router)
    return app
