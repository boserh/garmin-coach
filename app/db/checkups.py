"""User-scoped CRUD over ``HealthCheckup`` — the "Аналізи" tab's data layer. v1 is
plain storage (see the model docstring); analysis/reminders are future work over the
same rows, not a schema this module needs to anticipate."""
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HealthCheckup


async def list_checkups(session: AsyncSession, user_id: int) -> Sequence[HealthCheckup]:
    """Newest first — a checkup log reads back-to-front like an activity feed."""
    rows = await session.execute(
        select(HealthCheckup)
        .where(HealthCheckup.user_id == user_id)
        .order_by(HealthCheckup.date.desc(), HealthCheckup.id.desc())
    )
    return rows.scalars().all()


async def get_checkup(
    session: AsyncSession, user_id: int, checkup_id: int
) -> Optional[HealthCheckup]:
    """Scoped by user_id so one account can never read/edit another's record by
    guessing an id."""
    row = await session.execute(
        select(HealthCheckup).where(
            HealthCheckup.id == checkup_id, HealthCheckup.user_id == user_id
        )
    )
    return row.scalar_one_or_none()


async def create_checkup(
    session: AsyncSession,
    user_id: int,
    *,
    date: str,
    title: str,
    category: Optional[str] = None,
    results: Optional[list] = None,
    notes: Optional[str] = None,
    next_due_date: Optional[str] = None,
) -> HealthCheckup:
    row = HealthCheckup(
        user_id=user_id,
        date=date,
        title=title,
        category=category or None,
        results=results or None,
        notes=notes or None,
        next_due_date=next_due_date or None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_checkup(
    session: AsyncSession,
    row: HealthCheckup,
    *,
    date: str,
    title: str,
    category: Optional[str] = None,
    results: Optional[list] = None,
    notes: Optional[str] = None,
    next_due_date: Optional[str] = None,
) -> HealthCheckup:
    row.date = date
    row.title = title
    row.category = category or None
    row.results = results or None
    row.notes = notes or None
    row.next_due_date = next_due_date or None
    await session.commit()
    await session.refresh(row)
    return row


async def delete_checkup(session: AsyncSession, row: HealthCheckup) -> None:
    await session.delete(row)
    await session.commit()
