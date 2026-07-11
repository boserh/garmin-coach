"""EP-14 personal records: the pure-Python detector (distance/pace/week/VO2max/race),
the backfill-silent-vs-fresh-announce gate, the repository readers, and that records
enter the dedup-cache key."""
import datetime as dt

import pytest

from app import records
from app.analysis.service import _cache_key
from app.db.models import ActivityRecord, DailyMetric, PersonalRecord
from app.garmin import repository

U1 = 1


async def _run(session, aid, date, dist_km, dur_min, type="running"):
    a = ActivityRecord(user_id=U1, activity_id=aid, date=date, type=type,
                       dist_km=dist_km, dur_min=dur_min)
    session.add(a)
    await session.commit()
    await session.refresh(a)
    return a


async def _day(session, date, extra):
    session.add(DailyMetric(user_id=U1, date=date, extra=extra))
    await session.commit()


def _kinds(recs):
    return {r.kind for r in recs}


# --- distance / pace records --------------------------------------------------

async def test_detects_fastest_5k_and_longest(session):
    await _run(session, 1, "2026-07-10", 5.0, 30.0)   # 6:00/km
    new = await records.detect_records(session, U1)
    kinds = _kinds(new)
    assert "fastest_5k" in kinds
    assert "longest_run_km" in kinds
    assert "longest_run_min" in kinds
    pr = next(r for r in new if r.kind == "fastest_5k")
    assert pr.value == pytest.approx(6.0)
    assert pr.previous_value is None
    assert pr.activity_id is not None


async def test_faster_5k_beats_with_previous(session):
    await _run(session, 1, "2026-07-01", 5.0, 30.0)   # 6:00/km
    await records.detect_records(session, U1)
    await session.commit()
    await _run(session, 2, "2026-07-10", 5.05, 28.0)  # ~5:32/km, within ±5%
    new = await records.detect_records(session, U1)
    pr = next(r for r in new if r.kind == "fastest_5k")
    assert pr.value < 6.0
    assert pr.previous_value == pytest.approx(6.0)


async def test_slower_5k_is_no_record(session):
    await _run(session, 1, "2026-07-01", 5.0, 28.0)
    await records.detect_records(session, U1)
    await session.commit()
    await _run(session, 2, "2026-07-10", 5.0, 33.0)   # slower
    new = await records.detect_records(session, U1)
    assert "fastest_5k" not in _kinds(new)


async def test_detect_is_idempotent(session):
    await _run(session, 1, "2026-07-10", 10.0, 55.0)
    first = await records.detect_records(session, U1)
    await session.commit()
    assert first
    second = await records.detect_records(session, U1)
    assert second == []


async def test_distance_out_of_window_no_pace_record(session):
    # 7 km is neither a 5K (±5%) nor a 10K (±5%) → no pace record, but it is the longest.
    await _run(session, 1, "2026-07-10", 7.0, 42.0)
    new = _kinds(await records.detect_records(session, U1))
    assert "fastest_5k" not in new and "fastest_10k" not in new
    assert "longest_run_km" in new


async def test_garbage_pace_rejected(session):
    # 5 km in 10 min = 2:00/km, below the 2:30 floor → not a pace PB (still longest).
    await _run(session, 1, "2026-07-10", 5.0, 10.0)
    new = _kinds(await records.detect_records(session, U1))
    assert "fastest_5k" not in new


async def test_biggest_week(session):
    # Two runs in the same ISO week → their sum is the week record.
    await _run(session, 1, "2026-07-06", 8.0, 48.0)   # Monday
    await _run(session, 2, "2026-07-08", 6.0, 36.0)   # Wednesday
    new = await records.detect_records(session, U1)
    wk = next(r for r in new if r.kind == "biggest_week_km")
    assert wk.value == pytest.approx(14.0)
    assert wk.date == "2026-07-08"   # last run in the week


async def test_only_runs_counted(session):
    await _run(session, 1, "2026-07-10", 30.0, 90.0, type="cycling")
    new = _kinds(await records.detect_records(session, U1))
    assert new == set()


# --- metric records (VO2max / race predictions) -------------------------------

async def test_vo2max_record(session):
    await _day(session, "2026-07-05", {"vo2max": 45})
    await _day(session, "2026-07-10", {"vo2max": 47})
    new = await records.detect_records(session, U1)
    vo2 = next(r for r in new if r.kind == "vo2max")
    assert vo2.value == 47
    assert vo2.date == "2026-07-10"


async def test_race_prediction_needs_threshold(session):
    await _day(session, "2026-07-01", {"race_5k_s": 1600})
    await records.detect_records(session, U1)
    await session.commit()
    # 5 s better — under the 10 s noise floor → no new record.
    await _day(session, "2026-07-05", {"race_5k_s": 1595})
    assert "race_5k" not in _kinds(await records.detect_records(session, U1))
    # 15 s better — a real improvement.
    await _day(session, "2026-07-10", {"race_5k_s": 1585})
    new = await records.detect_records(session, U1)
    pr = next(r for r in new if r.kind == "race_5k")
    assert pr.value == 1585
    assert pr.previous_value == 1600


# --- announce gate (backfill silent) ------------------------------------------

async def test_announce_worthy_filters_old(session):
    today = dt.date(2026, 7, 11)
    fresh = PersonalRecord(kind="vo2max", value=47, date="2026-07-10")
    old = PersonalRecord(kind="longest_run_km", value=20, date="2026-06-01")
    got = records.announce_worthy([fresh, old], today=today)
    assert got == [fresh]


async def test_backfill_of_old_history_is_silent(session):
    # All runs are >FRESH_DAYS old → detected + seeded, but nothing to announce.
    await _run(session, 1, "2026-01-01", 5.0, 30.0)
    await _run(session, 2, "2026-02-01", 10.0, 60.0)
    new = await records.detect_records(session, U1)
    assert new                                    # records were seeded
    assert records.announce_worthy(new, today=dt.date(2026, 7, 11)) == []


# --- repository readers -------------------------------------------------------

async def test_current_records_latest_per_kind(session):
    await _run(session, 1, "2026-07-01", 5.0, 30.0)
    await records.detect_records(session, U1)
    await session.commit()
    await _run(session, 2, "2026-07-10", 5.0, 28.0)   # faster 5k → new row same kind
    await records.detect_records(session, U1)
    await session.commit()

    cur = await repository.current_records(session, U1)
    by_kind = {r.kind: r for r in cur}
    assert by_kind["fastest_5k"].value == pytest.approx(28.0 / 5.0)   # the newest best


async def test_recent_records_date_window(session):
    session.add(PersonalRecord(user_id=U1, kind="vo2max", value=47, date="2026-07-10"))
    session.add(PersonalRecord(user_id=U1, kind="longest_run_km", value=20,
                               date="2000-01-01"))
    await session.commit()
    recent = await repository.recent_records(session, U1, days=7)
    # Only the recent one falls in the window (cutoff is relative to today's real date).
    assert all(r.date >= (dt.date.today() - dt.timedelta(days=6)).isoformat()
               for r in recent)


# --- formatting + cache key ---------------------------------------------------

def test_format_value_shapes():
    assert records.format_value("fastest_5k", 5.5) == "5:30/км"
    assert records.format_value("race_marathon", 12345) == "3:25:45"
    assert records.format_value("race_5k", 1585) == "26:25"
    assert records.format_value("vo2max", 47.0) == "47"
    assert records.format_value("longest_run_km", 21.4) == "21.4 км"
    assert records.format_value("longest_run_min", 95.0) == "95 хв"


def test_celebrate_and_line():
    pr = PersonalRecord(kind="fastest_5k", value=5.5, previous_value=6.0, date="2026-07-10")
    line = records.format_record_line(pr)
    assert "5:30/км" in line and "було 6:00/км" in line
    msg = records.celebrate([pr])
    assert msg.startswith("🎉")


def test_records_change_cache_key():
    base = {"daily": [], "recent_activities": [], "planned_runs": []}
    ctx = records.to_context(
        [PersonalRecord(kind="vo2max", value=47, date="2026-07-10")]
    )
    k_without = _cache_key(base, "q", "m")
    k_with = _cache_key(base, "q", "m", records=ctx)
    assert k_without != k_with
