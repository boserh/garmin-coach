"""Per-request async session dependency for the web layer."""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import async_session_maker


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding one async session per request."""
    async with async_session_maker() as session:
        yield session
