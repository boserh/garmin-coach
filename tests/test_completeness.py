"""ST-18 · incomplete-day detection, per-user expected fields, the null-safe merge upsert,
and the build-time "which stored days to refetch" decision. Pure/DB, zero Garmin/LLM."""
import datetime as dt

from app import completeness
from app.garmin import repository, service
from app.garmin.schemas import DailySummary


def _full(date):
    return {"date": date, "sleep_score": 80, "hrv_avg": 55, "stress_avg": 30,
            "bb_charged": 40, "hrv_status": "BALANCED",
            "extra": {"resting_hr": 50, "readiness_score": 70}}


def _sleep_only(date):
    return {"date": date, "sleep_score": 80, "hrv_avg": None, "stress_avg": None,
            "bb_charged": None, "extra": {}}


# ---------- pure completeness ----------

def test_daily_completeness_full_row_is_complete():
    assert completeness.daily_completeness(_full("2026-07-20")) == set()


def test_daily_completeness_missing_fields():
    missing = completeness.daily_completeness(_sleep_only("2026-07-20"))
    assert missing == {"hrv_avg", "stress_avg", "bb_charged", "resting_hr", "readiness_score"}


def test_expected_fields_narrows_to_what_user_has():
    # a user who never produces readiness_score → it's not "expected", so a day missing
    # only that is still complete.
    history = [
        {"date": "2026-07-01", "sleep_score": 80, "hrv_avg": 55, "stress_avg": 30,
         "bb_charged": 40, "extra": {"resting_hr": 50}},   # no readiness ever
    ]
    exp = completeness.expected_fields(history)
    assert "readiness_score" not in exp and "resting_hr" in exp
    row = {"date": "2026-07-02", "sleep_score": 80, "hrv_avg": 55, "stress_avg": 30,
           "bb_charged": 40, "extra": {"resting_hr": 50}}
    assert completeness.daily_completeness(row, exp) == set()


def test_labels_are_ordered_and_ukrainian():
    lbls = completeness.labels({"readiness_score", "hrv_avg"})
    assert lbls == ["HRV", "готовність"]


# ---------- merge upsert (null-safe fill-only) ----------

async def test_upsert_daily_merge_fills_nulls_without_clobbering(session):
    # store a sleep-only day
    await repository.upsert_daily(session, 1, DailySummary(**_sleep_only("2026-07-20")))
    await session.commit()

    # a later fetch brings HRV/stress + a real resting_hr but sleep is momentarily None
    fresh = DailySummary(date="2026-07-20", sleep_score=None, hrv_avg=60, stress_avg=25,
                         bb_charged=None, extra={"resting_hr": 48, "readiness_score": 72})
    await repository.upsert_daily(session, 1, fresh, merge=True)
    await session.commit()

    rows = await repository.read_history(session, 1, days=40)
    row = next(r for r in rows if r["date"] == "2026-07-20")
    assert row["sleep_score"] == 80         # kept (fresh None never clobbers)
    assert row["hrv_avg"] == 60             # filled
    assert row["stress_avg"] == 25          # filled
    assert row["resting_hr"] == 48          # extra filled
    assert (row["extra"] or {}).get("readiness_score") == 72


async def test_upsert_daily_merge_keeps_existing_extra_value(session):
    await repository.upsert_daily(session, 2, DailySummary(
        date="2026-07-20", sleep_score=80, extra={"resting_hr": 50}))
    await session.commit()
    # a fresh fetch reports a DIFFERENT resting_hr — merge must NOT overwrite the stored one
    await repository.upsert_daily(session, 2, DailySummary(
        date="2026-07-20", extra={"resting_hr": 99, "readiness_score": 70}), merge=True)
    await session.commit()
    rows = await repository.read_history(session, 2, days=5)
    row = next(r for r in rows if r["date"] == "2026-07-20")
    assert row["resting_hr"] == 50   # untouched
    assert (row["extra"] or {}).get("readiness_score") == 70   # new key filled


# ---------- refetch decision ----------

async def test_incomplete_days_to_refetch(session):
    today = dt.date(2026, 7, 22)
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    old = (today - dt.timedelta(days=5)).isoformat()

    # 30-day history: a full day establishes all fields as "expected"
    await repository.upsert_daily(session, 3, DailySummary(**_full(old)))
    # yesterday stored incomplete (sleep only)
    await repository.upsert_daily(session, 3, DailySummary(**_sleep_only(yesterday)))
    await session.commit()

    cached = await repository.read_daily_metrics(session, 3, [yesterday, old])
    result = await service._incomplete_days_to_refetch(session, 3, cached, today)
    assert result == {yesterday}   # incomplete + in window
    # old (5 days) is out of the 2-day window even if incomplete
    assert old not in result


async def test_incomplete_days_complete_day_not_refetched(session):
    today = dt.date(2026, 7, 22)
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    await repository.upsert_daily(session, 4, DailySummary(**_full(yesterday)))
    await session.commit()
    cached = await repository.read_daily_metrics(session, 4, [yesterday])
    assert await service._incomplete_days_to_refetch(session, 4, cached, today) == set()
