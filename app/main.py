"""FastAPI application factory.

Run with::

    ./venv/bin/python -m uvicorn app.main:create_app --factory

The lifespan initialises the DB (create tables for a zero-config first run;
Alembic remains the source of truth) and disposes the engine on shutdown.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core import logging as app_logging
from app.db.base import dispose_db, init_db
from app.routers import admin, health, history, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await dispose_db()


def create_app() -> FastAPI:
    app_logging.setup()
    app = FastAPI(title="Garmin → Claude", version="1.0.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(reports.router)
    app.include_router(history.router)
    app.include_router(admin.router)
    return app
