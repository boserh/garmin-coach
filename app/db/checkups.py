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


async def merge_checkups(
    session: AsyncSession, user_id: int, checkup_ids: list
) -> Optional[HealthCheckup]:
    """Merge 2+ checkups into one: the newest (by date, then id as a tiebreak) survives
    and keeps its id, date/title/category/next_due_date — the others' results/notes are
    absorbed into it (oldest-to-newest reading order, exact-duplicate result rows
    skipped) and those other rows are then deleted. A stale ``analysis`` (narrated over
    the pre-merge numbers) is cleared, same as a plain edit. ``checkup_ids`` not owned
    by ``user_id`` are silently dropped (can't merge another account's records); returns
    ``None`` if fewer than 2 owned ids remain — nothing to merge."""
    rows = []
    for cid in checkup_ids:
        row = await get_checkup(session, user_id, cid)
        if row is not None:
            rows.append(row)
    if len(rows) < 2:
        return None

    rows.sort(key=lambda r: (r.date, r.id))  # oldest ... newest
    survivor = rows[-1]
    others = rows[:-1]

    combined_results: list = []
    combined_notes = []
    for r in rows:
        for item in (r.results or []):
            if item not in combined_results:
                combined_results.append(item)
        if r.notes:
            combined_notes.append(r.notes)

    survivor.results = combined_results or None
    survivor.notes = "\n\n".join(combined_notes) or None
    survivor.analysis = None

    for other in others:
        await session.delete(other)
    await session.commit()
    await session.refresh(survivor)
    return survivor


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


RECENT_CATEGORIES_DAYS = 365


async def recent_categories(
    session: AsyncSession, user_id: int, days: int = RECENT_CATEGORIES_DAYS
) -> list:
    """Distinct ``category`` (falling back to ``title`` when uncategorised) of checkups
    logged in the last ``days`` — so :func:`app.analysis.reports.run_supplement_advice`
    can skip recommending a test the user is already tracking. Newest-first, capped
    implicitly by the DISTINCT (a user rarely has more than a handful of categories)."""
    import datetime as dt

    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = await session.execute(
        select(HealthCheckup.category, HealthCheckup.title)
        .where(HealthCheckup.user_id == user_id, HealthCheckup.date >= since)
        .order_by(HealthCheckup.date.desc())
    )
    seen: list = []
    for category, title in rows.all():
        label = category or title
        if label and label not in seen:
            seen.append(label)
    return seen
