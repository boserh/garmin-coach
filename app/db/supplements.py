"""User-scoped CRUD over ``Supplement`` — the "Аналізи" tab's supplement list, feeding
``app.analysis.reports.run_supplement_advice`` (which lab markers to track and how
often, given what's currently being taken)."""
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Supplement


async def list_supplements(
    session: AsyncSession, user_id: int, *, active_only: bool = False
) -> Sequence[Supplement]:
    """Active first, then newest first — a stopped supplement stays visible for history
    but sinks below what's currently being taken."""
    conds = [Supplement.user_id == user_id]
    if active_only:
        conds.append(Supplement.is_active.is_(True))
    rows = await session.execute(
        select(Supplement).where(*conds)
        .order_by(Supplement.is_active.desc(), Supplement.id.desc())
    )
    return rows.scalars().all()


async def get_supplement(
    session: AsyncSession, user_id: int, supplement_id: int
) -> Optional[Supplement]:
    """Scoped by user_id so one account can never read/edit another's row by guessing
    an id."""
    row = await session.execute(
        select(Supplement).where(
            Supplement.id == supplement_id, Supplement.user_id == user_id
        )
    )
    return row.scalar_one_or_none()


async def create_supplement(
    session: AsyncSession,
    user_id: int,
    *,
    name: str,
    dosage: Optional[str] = None,
    frequency: Optional[str] = None,
    started_date: Optional[str] = None,
    notes: Optional[str] = None,
) -> Supplement:
    row = Supplement(
        user_id=user_id, name=name,
        dosage=dosage or None, frequency=frequency or None,
        started_date=started_date or None, notes=notes or None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_supplement(
    session: AsyncSession,
    row: Supplement,
    *,
    name: str,
    dosage: Optional[str] = None,
    frequency: Optional[str] = None,
    started_date: Optional[str] = None,
    notes: Optional[str] = None,
    is_active: bool = True,
) -> Supplement:
    row.name = name
    row.dosage = dosage or None
    row.frequency = frequency or None
    row.started_date = started_date or None
    row.notes = notes or None
    row.is_active = is_active
    await session.commit()
    await session.refresh(row)
    return row


async def delete_supplement(session: AsyncSession, row: Supplement) -> None:
    await session.delete(row)
    await session.commit()
