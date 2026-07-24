"""OPS-04 · the job-run log: record_job_run (insert/aggregate/rotation), the readers, and
the for_each_user recording wrapper (per-user isolation, tick aggregation)."""
import datetime as dt
from contextlib import asynccontextmanager

from app.db import job_runs
from app.db.models import JobRun


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
    from types import SimpleNamespace

    from bot import jobs as jobs_module
    from bot.jobs import JobOutcome, for_each_user

    u1 = SimpleNamespace(id=101, timezone="Europe/Warsaw")
    u2 = SimpleNamespace(id=102, timezone="Europe/Warsaw")

    async def fake_eligible(_s, *, with_chat=False):
        return [u1, u2]

    @asynccontextmanager
    async def fake_maker():
        yield session

    monkeypatch.setattr(jobs_module, "eligible_users", fake_eligible)
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
