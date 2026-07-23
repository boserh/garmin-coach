"""ST-15: manual resync of one activity and a range of days.

Covers the service layer (upsert-over with no duplicate, works for an activity older than
the fetch window, preserves subjective/analysis/step_match), the range cap, and the web
cross-user 404 isolation."""
import datetime as dt

import pytest
from sqlalchemy import func, select

from app.db.models import ActivityRecord, DailyMetric, User
from app.garmin import client, providers, service
from bot.handlers import _parse_resync_args

# ---------- fake Garmin provider ----------

class _Provider:
    username = "tester"
    display_name = "uuid-1234"

    def login(self):
        pass

    def connectapi(self, path, **kwargs):
        # Single-activity detail (nested summaryDTO / activityTypeDTO).
        if path.endswith("/details"):
            return {
                "metricDescriptors": [
                    {"key": "directSpeed", "metricsIndex": 0},
                    {"key": "directHeartRate", "metricsIndex": 1},
                    {"key": "sumDistance", "metricsIndex": 2},
                ],
                "activityDetailMetrics": [
                    {"metrics": [2.5, 150, 100]},
                    {"metrics": [3.0, 155, 1000]},
                ],
            }
        if "/activity-service/activity/" in path:
            return {
                "activityId": 111,
                "activityTypeDTO": {"typeKey": "running"},
                "summaryDTO": {
                    "startTimeLocal": "2026-06-21 07:00:00",
                    "duration": 1800, "distance": 5000,
                    "averageHR": 150, "maxHR": 165, "activityTrainingLoad": 80.0,
                },
            }
        # daily-summary endpoints (resync_days)
        if "dailySleepData" in path:
            return {"restingHeartRate": 48, "dailySleepDTO": {
                "sleepScores": {"overall": {"value": 82}},
                "deepSleepSeconds": 3600, "lightSleepSeconds": 7200,
                "remSleepSeconds": 5400, "awakeSleepSeconds": 600,
            }}
        if path.startswith("/hrv-service"):
            return {"hrvSummary": {"lastNightAvg": 60, "status": "BALANCED"}}
        if "dailyStress" in path:
            return {"avgStressLevel": 25, "maxStressLevel": 70}
        if "trainingreadiness" in path:
            return [{"score": 63, "level": "MODERATE"}]
        if "bodyBattery" in path:
            return [{"charged": 50, "drained": 40}]
        return {}


@pytest.fixture(autouse=True)
def _no_disk_cache_no_limiter(monkeypatch):
    """Keep the series/exercise disk cache out of the way and disable the rate-limiter so
    the tests never touch real files or sleep."""
    monkeypatch.setattr(client, "_limiter", client._RateLimiter(rps=0))
    monkeypatch.setattr(client, "_cache_get", lambda k: None)
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: None)


async def _seed_user(session) -> int:
    user = User(email="u@e.com", password_hash="h")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user.id


def _bind():
    return providers.set_current_provider(_Provider())


# ---------- resync_activity ----------

async def test_resync_activity_updates_in_place_no_duplicate(session):
    uid = await _seed_user(session)
    # A stale row with old numbers + subjective/analysis/step_match that must survive.
    session.add(ActivityRecord(
        user_id=uid, activity_id=111, date="2026-06-21", type="running",
        dur_min=99.0, dist_km=1.0, avg_hr=99, max_hr=99, load=1.0,
        subjective={"rpe": 7}, analysis="old writeup", step_match={"hits": 3},
    ))
    await session.commit()
    db_id = (await session.execute(
        select(ActivityRecord.id).where(ActivityRecord.activity_id == 111)
    )).scalar_one()

    token = _bind()
    try:
        updated = await service.resync_activity(session, uid, db_id)
    finally:
        providers.reset_current_provider(token)

    assert updated is not None
    # Fresh summary overwrote the stale values.
    assert updated.dur_min == 30.0 and updated.dist_km == 5.0
    assert updated.avg_hr == 150 and updated.max_hr == 165
    # A running activity got its series refetched.
    assert updated.series and updated.series[0].get("p") is not None
    # Subjective / analysis / step_match are never touched by a resync.
    assert updated.subjective == {"rpe": 7}
    assert updated.analysis == "old writeup"
    assert updated.step_match == {"hits": 3}
    # No duplicate row was created.
    n = (await session.execute(
        select(func.count()).select_from(ActivityRecord).where(ActivityRecord.user_id == uid)
    )).scalar_one()
    assert n == 1


async def test_resync_activity_wrong_user_returns_none(session):
    uid = await _seed_user(session)
    other = User(email="o@e.com", password_hash="h")
    session.add(other)
    await session.commit()
    await session.refresh(other)
    session.add(ActivityRecord(user_id=uid, activity_id=111, date="2026-06-21", type="running"))
    await session.commit()
    db_id = (await session.execute(
        select(ActivityRecord.id).where(ActivityRecord.activity_id == 111)
    )).scalar_one()

    token = _bind()
    try:
        assert await service.resync_activity(session, other.id, db_id) is None
    finally:
        providers.reset_current_provider(token)


async def test_resync_activity_missing_row_returns_none(session):
    uid = await _seed_user(session)
    token = _bind()
    try:
        assert await service.resync_activity(session, uid, 999) is None
    finally:
        providers.reset_current_provider(token)


# ---------- resync_days ----------

async def test_resync_days_overwrites_stale_row(session):
    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    uid = await _seed_user(session)
    await repository.upsert_daily(
        session, uid, DailySummary(date="2026-06-21", hrv_avg=99, has_data=True))
    await session.commit()

    token = _bind()
    try:
        written, requested = await service.resync_days(
            session, uid, [dt.date(2026, 6, 21)])
    finally:
        providers.reset_current_provider(token)

    assert (written, requested) == (1, 1)
    n = (await session.execute(
        select(func.count()).select_from(DailyMetric).where(DailyMetric.user_id == uid)
    )).scalar_one()
    assert n == 1   # overwrote, not duplicated
    m = (await session.execute(
        select(DailyMetric).where(DailyMetric.date == "2026-06-21")
    )).scalar_one()
    assert m.hrv_avg == 60 and m.sleep_score == 82
    assert m.extra and m.extra.get("resting_hr") == 48   # extra rewritten too


def test_fetch_exercise_summary_force_bypasses_cache_and_parses_reps_weight(monkeypatch):
    """The bug this fixes: resync of an edited strength activity returned the cached (pre-edit)
    exercises. ``force=True`` must ignore the immutable-asset cache and refetch — and the fresh
    result must carry per-set reps + weight (grams→kg), not just a set count."""
    calls = {"n": 0}
    monkeypatch.setattr(client, "_cache_get", lambda k: {
        "active_sets": 1, "sets": {"OLD": {"count": 1, "reps": [10], "weight_kg": [20.0]}}})
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: None)

    class P:
        def connectapi(self, path, **kwargs):
            calls["n"] += 1
            return {"exerciseSets": [
                {"setType": "ACTIVE", "repetitionCount": 12, "weight": 22000.0,
                 "exercises": [{"category": "SQUAT", "name": "SQUAT"}]},
                {"setType": "REST"},
                {"setType": "ACTIVE", "repetitionCount": 12, "weight": 22000.0,
                 "exercises": [{"category": "SQUAT", "name": "SQUAT"}]},
            ]}

    monkeypatch.setattr(client, "get_provider", lambda: P())

    # Default: the cached (stale) value is returned, no Garmin call.
    cached = client.fetch_exercise_summary(111)
    assert calls["n"] == 0 and "old" in cached["sets"]
    # force=True: cache ignored, a fresh fetch happens with reps + weight captured.
    fresh = client.fetch_exercise_summary(111, force=True)
    assert calls["n"] == 1
    squat = next(iter(fresh["sets"].values()))
    assert squat["count"] == 2
    assert squat["reps"] == [12, 12]
    assert squat["weight_kg"] == [22.0, 22.0]   # grams → kg


async def test_resync_days_empty_range_is_noop(session):
    uid = await _seed_user(session)
    token = _bind()
    try:
        assert await service.resync_days(session, uid, []) == (0, 0)
    finally:
        providers.reset_current_provider(token)


# ---------- arg parsing / range cap ----------

def test_parse_resync_args_defaults_to_yesterday_today():
    today = dt.date(2026, 7, 23)
    dates, err = _parse_resync_args([], today)
    assert err is None
    assert dates == [dt.date(2026, 7, 22), dt.date(2026, 7, 23)]


def test_parse_resync_args_single_and_range():
    dates, err = _parse_resync_args(["2026-07-01"], dt.date(2026, 7, 23))
    assert err is None and dates == [dt.date(2026, 7, 1)]
    dates, err = _parse_resync_args(["2026-07-03", "2026-07-01"], dt.date(2026, 7, 23))
    assert err is None            # reversed range is swapped
    assert dates[0] == dt.date(2026, 7, 1) and dates[-1] == dt.date(2026, 7, 3)


def test_parse_resync_args_rejects_garbage_and_overlong_range():
    assert _parse_resync_args(["not-a-date"], dt.date(2026, 7, 23)) == (None, "format")
    dates, err = _parse_resync_args(["2026-01-01", "2026-12-31"], dt.date(2026, 7, 23))
    assert dates is None and err == "range"
