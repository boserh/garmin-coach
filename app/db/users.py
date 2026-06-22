"""User-account queries, kept apart from the Garmin history repository."""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


async def get_by_email(session: AsyncSession, email: str) -> Optional[User]:
    return (
        await session.execute(select(User).where(User.email == email.lower().strip()))
    ).scalar_one_or_none()


async def get_by_chat_id(session: AsyncSession, chat_id: int) -> Optional[User]:
    return (
        await session.execute(select(User).where(User.telegram_chat_id == chat_id))
    ).scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    is_admin: bool = False,
) -> User:
    user = User(email=email.lower().strip(), password_hash=password_hash, is_admin=is_admin)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user
