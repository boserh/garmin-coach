"""SQLAlchemy 2.0 async foundation: declarative Base, engine, session factory.

The engine is driven entirely by ``settings.DATABASE_URL`` — SQLite (async,
zero-config) by default, switchable to Postgres (asyncpg) via the env var alone.
"""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base; all ORM models inherit from this and share its metadata."""


engine: AsyncEngine = create_async_engine(settings.DATABASE_URL, future=True)

if engine.dialect.name == "sqlite":
    # WAL lets readers and a writer touch the file concurrently instead of the default
    # rollback-journal's single-writer-blocks-everyone behavior; busy_timeout makes a
    # write that DOES collide wait and retry instead of raising "database is locked"
    # immediately. Needed now that garmin-web runs as >1 gunicorn worker process
    # (each with its own connection) against the same sqlite file.
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

async_session_maker = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    """Create tables for a zero-config first run. Alembic remains the source of
    truth for schema changes; this just makes the app runnable out of the box."""
    import app.db.models  # noqa: F401 — register models on Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_db() -> None:
    await engine.dispose()
