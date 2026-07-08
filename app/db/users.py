"""User-account queries, kept apart from the Garmin history repository."""
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


async def eligible_users(
    session: AsyncSession, *, with_chat: bool = False
) -> Sequence[User]:
    """Active + approved users — the recipient set every scheduled per-user job loops
    over. ``with_chat`` additionally requires a Telegram chat id (jobs that DM)."""
    conds = [User.is_active.is_(True), User.is_approved.is_(True)]
    if with_chat:
        conds.append(User.telegram_chat_id.is_not(None))
    return (await session.execute(select(User).where(*conds))).scalars().all()


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
    is_approved: bool = True,
) -> User:
    """Create a user. ``is_approved`` defaults to True (admin/CLI creation); the
    self-registration path passes False so an admin must approve first."""
    user = User(
        email=email.lower().strip(),
        password_hash=password_hash,
        is_admin=is_admin,
        is_approved=is_approved,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user
