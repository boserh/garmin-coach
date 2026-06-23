"""Repository upsert idempotency, per-user isolation, and history reads."""
from sqlalchemy import func, select

from app.db.models import ActivityRecord, DailyMetric, ReportLog
from app.garmin import repository
from app.garmin.schemas import DailySummary

U1, U2 = 1, 2  # FK enforcement is off in SQLite, so bare ids are fine for unit tests


async def _count(session, model):
    return (await session.execute(select(func.count(model.id)))).scalar_one()


async def test_upsert_daily_is_idempotent(session):
    s1 = DailySummary(date="2026-06-20", sleep_score=80, hrv_avg=55,
                      stress_avg=20, has_data=True)
    await repository.upsert_daily(session, U1, s1)
    await session.commit()

    # same (user, date), new values → update in place, not a second row
    s2 = DailySummary(date="2026-06-20", sleep_score=90, hrv_avg=58,
                      stress_avg=22, has_data=True)
    await repository.upsert_daily(session, U1, s2)
    await session.commit()

    assert await _count(session, DailyMetric) == 1
    got = await repository.read_daily_metrics(session, U1, ["2026-06-20"])
    assert got["2026-06-20"].sleep_score == 90
    assert got["2026-06-20"].hrv_avg == 58


async def test_daily_metrics_isolated_per_user(session):
    await repository.upsert_daily(
        session, U1, DailySummary(date="2026-06-20", hrv_avg=55, has_data=True))
    await repository.upsert_daily(
        session, U2, DailySummary(date="2026-06-20", hrv_avg=70, has_data=True))
    await session.commit()

    # same date, two users → two rows, each sees only its own
    assert await _count(session, DailyMetric) == 2
    got1 = await repository.read_daily_metrics(session, U1, ["2026-06-20"])
    got2 = await repository.read_daily_metrics(session, U2, ["2026-06-20"])
    assert got1["2026-06-20"].hrv_avg == 55
    assert got2["2026-06-20"].hrv_avg == 70


async def test_upsert_activity_is_idempotent(session):
    row = {"date": "2026-06-20", "type": "strength_training", "dur_min": 45.0,
           "dist_km": 0.0, "avg_hr": 110, "max_hr": 140, "load": 60.0,
           "exercises": {"active_sets": 12, "sets": {"присідання": 4}}}
    await repository.upsert_activity(session, U1, 111, row)
    await session.commit()

    row2 = dict(row, load=75.0)
    await repository.upsert_activity(session, U1, 111, row2)
    await session.commit()

    assert await _count(session, ActivityRecord) == 1
    rec = (await session.execute(
        select(ActivityRecord).where(ActivityRecord.activity_id == 111)
    )).scalar_one()
    assert rec.load == 75.0
    assert rec.exercises == {"active_sets": 12, "sets": {"присідання": 4}}


async def test_upsert_activity_skips_when_no_id(session):
    await repository.upsert_activity(session, U1, None, {"date": "2026-06-20"})
    await session.commit()
    assert await _count(session, ActivityRecord) == 0


async def test_log_report_stores_text(session):
    await repository.log_report(
        session, user_id=U1, kind="report", model="claude-sonnet-4-6",
        input_tokens=10, output_tokens=5, cost_usd=0.001, ok=True,
        report_text="🟢 hello report",
    )
    rows = (await session.execute(select(ReportLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].report_text == "🟢 hello report"
    assert rows[0].user_id == U1


async def test_get_last_report(session):
    import datetime as dt

    from sqlalchemy import select

    from app.db.models import ReportLog

    assert await repository.get_last_report(session, U1) is None
    # failed calls and null-text rows are ignored
    await repository.log_report(session, user_id=U1, kind="report", model="m",
                                ok=False, error="boom")
    # today's report is NOT day-over-day context (keeps the dedup key stable on
    # repeated same-day /report), and /deep is excluded entirely
    await repository.log_report(session, user_id=U1, kind="report", model="m",
                                ok=True, report_text="сьогоднішній")
    await repository.log_report(session, user_id=U1, kind="deep", model="m",
                                ok=True, report_text="глибокий")
    assert await repository.get_last_report(session, U1) is None

    # a daily report from yesterday IS the context
    await repository.log_report(
        session, user_id=U1, kind="morning", model="m", ok=True,
        report_text="🟢 учора все ок")
    yesterday = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    row = (await session.execute(
        select(ReportLog).where(ReportLog.report_text == "🟢 учора все ок")
    )).scalar_one()
    row.created_at = yesterday
    await session.commit()

    # another user's report must not leak in
    await repository.log_report(
        session, user_id=U2, kind="report", model="m", ok=True, report_text="чужий")

    text, date = await repository.get_last_report(session, U1)
    assert text == "🟢 учора все ок"
    assert date == yesterday.date().isoformat()


async def test_get_recent_reports_filters_and_orders(session):
    await repository.log_report(session, user_id=U1, kind="report", model="m",
                                ok=False, error="x")
    await repository.log_report(session, user_id=U1, kind="deep", model="m", ok=True,
                                report_text="глибокий розбір")
    await repository.log_report(session, user_id=U1, kind="ask", model="m", ok=True,
                                report_text="відповідь на питання")
    await repository.log_report(session, user_id=U1, kind="report", model="m", ok=True,
                                report_text="звіт A")
    await repository.log_report(session, user_id=U1, kind="report", model="m", ok=True,
                                report_text="звіт B")
    await repository.log_report(session, user_id=U2, kind="report", model="m", ok=True,
                                report_text="чужий звіт")

    recent = await repository.get_recent_reports(session, U1, n=3)
    texts = [r["text"] for r in recent]
    assert texts == ["звіт B", "звіт A"]  # deep/ask/failed/other-user excluded
    assert all("date" in r for r in recent)


async def test_get_recent_asks(session):
    # only this user's successful asks, with question + answer, within the window
    await repository.log_report(session, user_id=U1, kind="report", model="m", ok=True,
                                question="звіт?", report_text="звіт — не ask")
    await repository.log_report(session, user_id=U1, kind="ask", model="m", ok=True,
                                question="чи бігти?", report_text="так, легко")
    await repository.log_report(session, user_id=U1, kind="ask", model="m", ok=False,
                                question="впав", error="boom")  # failed → excluded
    await repository.log_report(session, user_id=U2, kind="ask", model="m", ok=True,
                                question="чуже", report_text="чужа відповідь")

    asks = await repository.get_recent_asks(session, U1, minutes=5)
    assert asks == [{"question": "чи бігти?", "answer": "так, легко"}]

    # nothing in a zero-minute window (everything is older than 'now')
    assert await repository.get_recent_asks(session, U1, minutes=0) == []


async def test_state_is_per_user(session):
    await repository.set_state(session, U1, "morning_sent_date", "2026-06-22")
    await repository.set_state(session, U2, "morning_sent_date", "2026-06-21")
    assert await repository.get_state(session, U1, "morning_sent_date") == "2026-06-22"
    assert await repository.get_state(session, U2, "morning_sent_date") == "2026-06-21"
    assert await repository.get_state(session, U1, "missing") is None


async def test_read_history_orders_oldest_first(session):
    import datetime as dt
    today = dt.date.today()
    for i in (2, 0, 1):  # insert out of order
        d = (today - dt.timedelta(days=i)).isoformat()
        await repository.upsert_daily(
            session, U1, DailySummary(date=d, hrv_avg=50 + i, has_data=True)
        )
    await session.commit()

    trend = await repository.read_history(session, U1, days=7)
    dates = [r["date"] for r in trend]
    assert dates == sorted(dates)
    assert len(trend) == 3
