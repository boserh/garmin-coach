"""OPS-04 · the job-run log: record_job_run (insert/aggregate/rotation), the readers, and
the for_each_user recording wrapper (per-user isolation, tick aggregation)."""
import datetime as dt
from contextlib import asynccontextmanager

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import job_runs
from app.db.base import Base
from app.db.models import JobRun, User


async def test_record_inserts_a_row(session):
    await job_runs.record_job_run(session, job="PLAN sync", user_id=1, status="ok",
                                  detail="synced", run_date="2026-07-24")
    await session.commit()
    rows = await job_runs.recent_job_runs(session, user_id=1)
    assert len(rows) == 1
    assert rows[0].job == "PLAN sync" and rows[0].status == "ok" and rows[0].count == 1


async def test_aggregate_folds_routine_ticks(session):
    for _ in range(5):
        await job_runs.record_job_run(session, job="MORNING", user_id=1, status="ok",
                                      detail="tick", run_date="2026-07-24", aggregate=True)
    await session.commit()
    rows = await job_runs.recent_job_runs(session, user_id=1)
    assert len(rows) == 1              # five ticks folded into ONE row
    assert rows[0].count == 5

    # a different day starts a fresh aggregate row
    await job_runs.record_job_run(session, job="MORNING", user_id=1, status="ok",
                                  detail="tick", run_date="2026-07-25", aggregate=True)
    await session.commit()
    rows = await job_runs.recent_job_runs(session, user_id=1)
    assert len(rows) == 2


async def test_notable_and_error_get_own_rows(session):
    # routine ticks aggregate...
    await job_runs.record_job_run(session, job="MORNING", user_id=1, status="ok",
                                  detail="tick", run_date="2026-07-24", aggregate=True)
    # ...a notable "sent" and an error are separate (aggregate=False)
    await job_runs.record_job_run(session, job="MORNING", user_id=1, status="ok",
                                  detail="morning report sent", run_date="2026-07-24")
    await job_runs.record_job_run(session, job="MORNING", user_id=1, status="error",
                                  detail="boom", run_date="2026-07-24")
    await session.commit()
    rows = await job_runs.recent_job_runs(session, user_id=1)
    assert len(rows) == 3
    assert {r.status for r in rows} == {"ok", "error"}


async def test_recent_filters_by_user_and_job(session):
    await job_runs.record_job_run(session, job="MORNING", user_id=1, status="ok")
    await job_runs.record_job_run(session, job="DIGEST", user_id=1, status="ok")
    await job_runs.record_job_run(session, job="MORNING", user_id=2, status="ok")
    await session.commit()

    assert len(await job_runs.recent_job_runs(session, user_id=1)) == 2
    assert len(await job_runs.recent_job_runs(session, user_id=1, job="MORNING")) == 1
    assert len(await job_runs.recent_job_runs(session)) == 3   # admin view: all users


async def test_rotation_purges_old_rows(session):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)
    session.add(JobRun(job="MORNING", user_id=1, status="ok", count=1,
                       started_at=old, finished_at=old))
    await session.commit()
    # any write triggers the lazy purge of >30-day rows
    await job_runs.record_job_run(session, job="MORNING", user_id=1, status="ok")
    await session.commit()
    rows = await job_runs.recent_job_runs(session, user_id=1)
    assert len(rows) == 1 and rows[0].status == "ok"   # the 40-day-old row was purged


async def test_last_job_status(session):
    await job_runs.record_job_run(session, job="MORNING", user_id=1, status="skip",
                                  detail="outside window")
    await job_runs.record_job_run(session, job="MORNING", user_id=1, status="ok",
                                  detail="morning report sent")
    await session.commit()
    last = await job_runs.last_job_status(session, 1, "MORNING")
    assert last is not None and last.detail == "morning report sent"


# ---------- for_each_user recording ----------

async def test_for_each_user_records_outcomes(session, monkeypatch):
    from bot import jobs as jobs_module
    from bot.jobs import JobOutcome, for_each_user

    # Real rows, not stand-ins: the scaffold re-loads each user in their own session.
    u1 = User(email="a@e.com", password_hash="h", is_approved=True, telegram_chat_id=1)
    u2 = User(email="b@e.com", password_hash="h", is_approved=True, telegram_chat_id=2)
    session.add_all([u1, u2])
    await session.commit()

    @asynccontextmanager
    async def fake_maker():
        yield session

    monkeypatch.setattr(jobs_module, "async_session_maker", fake_maker)

    async def worker(_s, user):
        if user.id == u1.id:
            return JobOutcome("skip", "no Garmin credentials")
        raise RuntimeError("kaboom")

    await for_each_user(worker, with_chat=True, label="TESTJOB")

    rows = await job_runs.recent_job_runs(session, job="TESTJOB")
    by_user = {r.user_id: r for r in rows}
    assert by_user[u1.id].status == "skip" and by_user[u1.id].detail == "no Garmin credentials"
    assert by_user[u2.id].status == "error" and "kaboom" in (by_user[u2.id].detail or "")


# ---------- the log write vs. the shared session (production shape) ----------
#
# The `session` fixture above is in-memory with a StaticPool: every session in a test
# shares ONE connection, so a second session can never collide with the first one's open
# transaction, and no ORM instance is ever a real expired row. Production is a file-backed
# SQLite (WAL + busy_timeout) where the recorder's session is a SECOND connection — these
# two tests rebuild that shape, which is where the log write actually failed.


@pytest_asyncio.fixture
async def prod_maker(tmp_path):
    """A file-backed engine wired like app.db.base (WAL + busy_timeout), one connection
    per session. busy_timeout is short here (prod waits 5s) so a lock fails fast."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/jobs.db")

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=300")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(maker, n: int) -> list:
    async with maker() as s:
        users = [User(email=f"u{i}@e.com", password_hash="h", is_approved=True,
                      telegram_chat_id=i + 1) for i in range(n)]
        for u in users:
            s.add(u)
        await s.commit()
        return [u.id for u in users]


async def test_records_the_row_when_a_worker_left_a_write_uncommitted(prod_maker, monkeypatch):
    """_tick_for_user catches its own exceptions and RETURNS an error outcome, so the
    worker's session can still hold an interrupted (flushed, uncommitted) write when the
    recorder runs. The recorder's own connection must not block on that write lock —
    5s of busy_timeout later it raised "database is locked" and the row was lost."""
    from bot import jobs as jobs_module
    from bot.jobs import JobOutcome, for_each_user

    uid = (await _seed(prod_maker, 1))[0]
    monkeypatch.setattr(jobs_module, "async_session_maker", prod_maker)

    async def worker(session, user):
        user.weather_location = "Kyiv"
        await session.execute(select(User))      # autoflush → open write transaction
        return JobOutcome("error", "boom", notable=True)

    await for_each_user(worker, with_chat=True, label="TESTLOCK")

    async with prod_maker() as s:
        rows = await job_runs.recent_job_runs(s, job="TESTLOCK")
    assert [(r.user_id, r.status) for r in rows] == [(uid, "error")]


async def test_records_rows_when_a_worker_raises_before_committing(prod_maker, monkeypatch):
    """A worker that raises before its first commit leaves a session that has to be rolled
    back, which EXPIRES its User instance. Nothing after that may read the instance lazily
    (MissingGreenlet) — that lost the row AND killed the rest of the loop with it."""
    from bot import jobs as jobs_module
    from bot.jobs import for_each_user

    ids = await _seed(prod_maker, 2)
    monkeypatch.setattr(jobs_module, "async_session_maker", prod_maker)

    seen = []

    async def worker(_session, user):
        seen.append(user.id)
        raise RuntimeError("boom")

    await for_each_user(worker, with_chat=True, label="TESTEXPIRE")

    assert seen == ids                       # one user's failure never aborts the loop
    async with prod_maker() as s:
        rows = await job_runs.recent_job_runs(s, job="TESTEXPIRE")
    assert sorted(r.user_id for r in rows) == ids
    assert {r.status for r in rows} == {"error"}
