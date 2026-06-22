"""Repository upsert idempotency + history reads against in-memory SQLite."""
from sqlalchemy import func, select

from app.db.models import ActivityRecord, DailyMetric, ReportLog
from app.garmin import repository
from app.garmin.schemas import DailySummary


async def _count(session, model):
    return (await session.execute(select(func.count(model.id)))).scalar_one()


async def test_upsert_daily_is_idempotent(session):
    s1 = DailySummary(date="2026-06-20", sleep_score=80, hrv_avg=55,
                      stress_avg=20, has_data=True)
    await repository.upsert_daily(session, s1)
    await session.commit()

    # same date, new values → update in place, not a second row
    s2 = DailySummary(date="2026-06-20", sleep_score=90, hrv_avg=58,
                      stress_avg=22, has_data=True)
    await repository.upsert_daily(session, s2)
    await session.commit()

    assert await _count(session, DailyMetric) == 1
    got = await repository.read_daily_metrics(session, ["2026-06-20"])
    assert got["2026-06-20"].sleep_score == 90
    assert got["2026-06-20"].hrv_avg == 58


async def test_upsert_activity_is_idempotent(session):
    row = {"date": "2026-06-20", "type": "strength_training", "dur_min": 45.0,
           "dist_km": 0.0, "avg_hr": 110, "max_hr": 140, "load": 60.0,
           "exercises": {"active_sets": 12, "sets": {"присідання": 4}}}
    await repository.upsert_activity(session, 111, row)
    await session.commit()

    row2 = dict(row, load=75.0)
    await repository.upsert_activity(session, 111, row2)
    await session.commit()

    assert await _count(session, ActivityRecord) == 1
    rec = (await session.execute(
        select(ActivityRecord).where(ActivityRecord.activity_id == 111)
    )).scalar_one()
    assert rec.load == 75.0
    assert rec.exercises == {"active_sets": 12, "sets": {"присідання": 4}}


async def test_upsert_activity_skips_when_no_id(session):
    await repository.upsert_activity(session, None, {"date": "2026-06-20"})
    await session.commit()
    assert await _count(session, ActivityRecord) == 0


async def test_log_report_stores_text(session):
    await repository.log_report(
        session, kind="report", model="claude-sonnet-4-6",
        input_tokens=10, output_tokens=5, cost_usd=0.001, ok=True,
        report_text="🟢 hello report",
    )
    rows = (await session.execute(select(ReportLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].report_text == "🟢 hello report"
    assert rows[0].kind == "report"


async def test_get_last_report_text(session):
    assert await repository.get_last_report_text(session) is None
    # failed calls and null-text rows are ignored
    await repository.log_report(session, kind="report", model="m", ok=False, error="boom")
    await repository.log_report(
        session, kind="morning", model="m", ok=True, report_text="🟢 учора все ок"
    )
    assert await repository.get_last_report_text(session) == "🟢 учора все ок"


async def test_read_history_orders_oldest_first(session):
    import datetime as dt
    today = dt.date.today()
    for i in (2, 0, 1):  # insert out of order
        d = (today - dt.timedelta(days=i)).isoformat()
        await repository.upsert_daily(
            session, DailySummary(date=d, hrv_avg=50 + i, has_data=True)
        )
    await session.commit()

    trend = await repository.read_history(session, days=7)
    dates = [r["date"] for r in trend]
    assert dates == sorted(dates)
    assert len(trend) == 3
