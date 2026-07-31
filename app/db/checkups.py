"""User-scoped CRUD over ``HealthCheckup`` — the "Аналізи" tab's data layer, plus the
two read helpers its follow-up features lean on: :func:`similar_history` (trend context
for Claude's interpretation) and :func:`due_for_reminder` (candidates for the
next-checkup nudge, filtered further by ``app.checkup_reminders``)."""
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
    # A stored interpretation was narrated over the OLD numbers — keep it around next to
    # numbers that no longer match would be actively misleading, so an edit clears it (the
    # detail page then shows the "Проаналізувати" button again instead of stale text).
    row.analysis = None
    await session.commit()
    await session.refresh(row)
    return row


async def delete_checkup(session: AsyncSession, row: HealthCheckup) -> None:
    await session.delete(row)
    await session.commit()


async def set_analysis(session: AsyncSession, row: HealthCheckup, text: str) -> None:
    row.analysis = text
    await session.commit()


async def similar_history(
    session: AsyncSession, user_id: int, row: HealthCheckup, limit: int = 3
) -> Sequence[HealthCheckup]:
    """Up to ``limit`` prior checkups (strictly before this one) sharing this row's
    category — or, when it has none, the exact same title — most-recent first. Trend
    context for :func:`app.analysis.reports.run_checkup_analysis` ("HRV was X last
    time, now Y")."""
    conds = [
        HealthCheckup.user_id == user_id,
        HealthCheckup.id != row.id,
        HealthCheckup.date < row.date,
    ]
    conds.append(
        HealthCheckup.category == row.category if row.category else HealthCheckup.title == row.title
    )
    rows = await session.execute(
        select(HealthCheckup).where(*conds).order_by(HealthCheckup.date.desc()).limit(limit)
    )
    return rows.scalars().all()


async def due_for_reminder(session: AsyncSession, user_id: int) -> Sequence[HealthCheckup]:
    """This user's checkups that carry a ``next_due_date`` — the candidate set
    :mod:`app.checkup_reminders` filters down to what's actually due/overdue."""
    rows = await session.execute(
        select(HealthCheckup).where(
            HealthCheckup.user_id == user_id, HealthCheckup.next_due_date.is_not(None)
        )
    )
    return rows.scalars().all()
