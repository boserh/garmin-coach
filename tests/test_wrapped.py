"""NF-07 quarterly/yearly Wrapped: the pure period helpers, the repository aggregate
(wrapped_stats + records_in_range), and the run_wrapped service call (None when the window
is too empty; cache hit on repeat; ReportLog written)."""
import datetime as dt
from unittest.mock import patch

from sqlalchemy import select

from app import wrapped
from app.analysis import reports, service
from app.analysis.service import CallStats, _wrapped_cache_key, run_wrapped
from app.db.models import ActivityRecord, DailyMetric, PersonalRecord, ReportLog
from app.garmin import repository

U1 = 1


# --- period parsing / window --------------------------------------------------

def test_parse_period_default_and_quarter():
    assert wrapped.parse_period(None) == "year"
    assert wrapped.parse_period([]) == "year"
    assert wrapped.parse_period(["banana"]) == "year"
    assert wrapped.parse_period(["quarter"]) == "quarter"
    assert wrapped.parse_period(["квартал"]) == "quarter"
    assert wrapped.parse_period(["q"]) == "quarter"


def test_period_window_lengths():
    today = dt.date(2026, 7, 20)
    ys, ye = wrapped.period_window(today, "year")
    assert ye == "2026-07-20"
    assert ys == "2025-07-22"                 # 52 weeks (364 days) inclusive
    qs, qe = wrapped.period_window(today, "quarter")
    assert qs == "2026-04-21"                 # 13 weeks inclusive


def test_label_and_fmt_range():
    assert wrapped.label("year") == "рік"
    assert wrapped.label("quarter") == "квартал"
    assert wrapped.fmt_range("2025-08-01", "2026-07-20") == "1 серпня 2025 – 20 липня 2026"


def test_has_signal():
    assert wrapped.has_signal({"runs": 5, "run_km": 40}) is True
    assert wrapped.has_signal({"runs": 1, "run_km": 0}) is False
    assert wrapped.has_signal({}) is False


# --- repository.wrapped_stats / records_in_range ------------------------------

async def test_wrapped_stats_aggregates_period(session):
    session.add_all([
        ActivityRecord(user_id=U1, activity_id=1, date="2026-06-01",
                       type="running", dist_km=10.0, dur_min=55.0, avg_hr=150),
        ActivityRecord(user_id=U1, activity_id=2, date="2026-06-03",
                       type="running", dist_km=5.0, dur_min=30.0, avg_hr=148),  # same ISO week
        ActivityRecord(user_id=U1, activity_id=3, date="2026-06-20",
                       type="cycling", dist_km=30.0, dur_min=60.0, avg_hr=130),
    ])
    session.add_all([
        DailyMetric(user_id=U1, date="2026-01-05", extra={"vo2max": 45}),   # arc start
        DailyMetric(user_id=U1, date="2026-06-30", extra={"vo2max": 49}),   # arc end
    ])
    await session.commit()

    s = await repository.wrapped_stats(session, U1, "2026-01-01", "2026-07-20")
    assert s["runs"] == 2 and s["run_km"] == 15.0
    assert s["total_activities"] == 3
    assert s["sports"]["run"] == 2 and s["sports"]["bike"] == 1
    assert s["biggest_week"]["km"] == 15.0            # both runs are the same ISO week
    assert s["vo2_start"] == 45.0 and s["vo2_end"] == 49.0
    assert round(s["total_hours"], 1) == round((55 + 30 + 60) / 60, 1)


async def test_records_in_range(session):
    session.add_all([
        PersonalRecord(user_id=U1, kind="fastest_5k", value=1500, date="2026-03-01"),
        PersonalRecord(user_id=U1, kind="longest_run", value=21.1, date="2026-06-15"),
        PersonalRecord(user_id=U1, kind="fastest_5k", value=1600, date="2025-01-01"),  # out
    ])
    await session.commit()
    rows = await repository.records_in_range(session, U1, "2026-01-01", "2026-07-20")
    assert [r.kind for r in rows] == ["longest_run", "fastest_5k"]   # newest first


# --- run_wrapped service ------------------------------------------------------

async def _wrapped_logs(session):
    return list((await session.execute(
        select(ReportLog).where(ReportLog.kind == "wrapped")
    )).scalars().all())


async def test_run_wrapped_none_without_history(session):
    text = await run_wrapped(session, user_id=U1, period="year")
    assert text is None
    assert await _wrapped_logs(session) == []


async def test_run_wrapped_narrates_and_logs(session):
    today = dt.date.today()
    start, _ = wrapped.period_window(today, "year")
    session.add_all([
        ActivityRecord(user_id=U1, activity_id=10, date=today.isoformat(),
                       type="running", dist_km=5.0, dur_min=28.0, avg_hr=150),
        ActivityRecord(user_id=U1, activity_id=11, date=start,
                       type="running", dist_km=8.0, dur_min=45.0, avg_hr=150),
    ])
    await session.commit()

    stats = CallStats(kind="wrapped", model=service.MODEL_WRAPPED,
                      input_tokens=60, output_tokens=40, cost_usd=0.01)
    with patch.object(reports, "wrapped_with_stats",
                      return_value=("твій рік був вогонь", stats)) as m:
        text = await run_wrapped(session, user_id=U1, period="year", api_key="k")

    assert text == "твій рік був вогонь"
    m.assert_called_once()
    logs = await _wrapped_logs(session)
    assert len(logs) == 1 and logs[0].ok is True and logs[0].cached is False


async def test_run_wrapped_cache_hit_on_repeat(session):
    today = dt.date.today()
    session.add(ActivityRecord(user_id=U1, activity_id=30, date=today.isoformat(),
                               type="running", dist_km=6.0, dur_min=33.0, avg_hr=150))
    await session.commit()

    stats = CallStats(kind="wrapped", model=service.MODEL_WRAPPED)
    with patch.object(reports, "wrapped_with_stats", return_value=("з кешу", stats)) as m:
        first = await run_wrapped(session, user_id=U1, period="year")
        second = await run_wrapped(session, user_id=U1, period="year")

    assert first == second == "з кешу"
    m.assert_called_once()
    logs = await _wrapped_logs(session)
    assert len(logs) == 2 and logs[1].cached is True


# --- dedup-cache key ----------------------------------------------------------

def test_wrapped_cache_key_reflects_stats():
    base = {"period": "year", "start": "2025-08-01", "end": "2026-07-20",
            "stats": {"run_km": 800}, "records": []}
    k1 = _wrapped_cache_key(base, "claude-opus-4-8")
    k2 = _wrapped_cache_key({**base, "stats": {"run_km": 900}}, "claude-opus-4-8")
    assert k1 != k2
