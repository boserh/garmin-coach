"""OPS-04 · async read/write over the ``job_runs`` background-job log.

Mirrors ``app.db.llm_cache``'s shape (a thin async data layer with lazy retention-purge on
write). The recorder is called once per per-user job branch from ``bot.jobs.for_each_user``;
the readers back ``/me/jobs`` and ``/admin/jobs`` plus ``/status``'s last-morning line.
"""
import datetime as dt
from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobRun

RETENTION_DAYS = 30


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def record_job_run(
    session: AsyncSession, *, job: str, user_id: Optional[int], status: str,
    detail: Optional[str] = None, run_date: Optional[str] = None,
    aggregate: bool = False, started_at: Optional[dt.datetime] = None,
) -> None:
    """Write one job-run row (does not commit). With ``aggregate=True`` a routine run rolls
    into the existing (job, user_id, run_date) row for the day — incrementing ``count`` and
    refreshing status/detail/finished_at — instead of adding a new row, so the 20-min tick
    doesn't flood the log. Notable outcomes and errors pass ``aggregate=False`` for their own
    row. Rows older than ``RETENTION_DAYS`` are purged lazily here."""
    now = _utcnow()
    detail = detail[:512] if detail else None
    cutoff = now - dt.timedelta(days=RETENTION_DAYS)
    # synchronize_session=False: don't evaluate the criterion against pending in-session
    # objects (SQLite stores naive datetimes, which can't compare with our aware cutoff).
    await session.execute(
        delete(JobRun).where(JobRun.started_at < cutoff)
        .execution_options(synchronize_session=False)
    )

    if aggregate and run_date is not None:
        existing = (await session.execute(
            select(JobRun).where(
                JobRun.job == job, JobRun.user_id == user_id,
                JobRun.run_date == run_date, JobRun.count.is_not(None),
            ).order_by(JobRun.started_at.desc()).limit(1)
        )).scalar_one_or_none()
        # Only fold into a row that is itself an aggregate (count-bearing) one; a notable/error
        # row for the same day keeps its own identity.
        if existing is not None:
            existing.count = (existing.count or 1) + 1
            existing.status = status
            existing.detail = detail
            existing.finished_at = now
            return

    session.add(JobRun(
        job=job, user_id=user_id, status=status, detail=detail, count=1,
        run_date=run_date, started_at=started_at or now, finished_at=now,
    ))


async def recent_job_runs(
    session: AsyncSession, *, user_id: Optional[int] = None, job: Optional[str] = None,
    limit: int = 50,
) -> List[JobRun]:
    """Most recent job-run rows, newest first. ``user_id`` scopes to one user (``/me/jobs``);
    omit it for the admin view (all users). ``job`` filters by job label."""
    stmt = select(JobRun)
    if user_id is not None:
        stmt = stmt.where(JobRun.user_id == user_id)
    if job:
        stmt = stmt.where(JobRun.job == job)
    stmt = stmt.order_by(JobRun.started_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def last_job_status(
    session: AsyncSession, user_id: int, job: str
) -> Optional[JobRun]:
    """The single most recent run of ``job`` for this user (``/status`` last-morning line)."""
    return (await session.execute(
        select(JobRun).where(JobRun.user_id == user_id, JobRun.job == job)
        .order_by(JobRun.started_at.desc()).limit(1)
    )).scalar_one_or_none()
