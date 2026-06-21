"""Test setup: point the app at a throwaway SQLite file before anything imports
the engine, and provide an isolated in-memory session fixture."""
import os

# Must run before any app.* import pulls in the engine from Settings.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_garmin.db")
os.environ.setdefault("WEB_TOKEN", "")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.db.models  # noqa: E402,F401 — register models on Base.metadata
from app.db.base import Base  # noqa: E402


@pytest_asyncio.fixture
async def session():
    """A fresh in-memory SQLite session per test (shared connection via StaticPool)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()
