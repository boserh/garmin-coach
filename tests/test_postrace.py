"""NF-23: the post-race debrief — pure numbers, the degradation paths, and the job branch.

The Claude narration is mocked everywhere (the suite spends $0); what's tested here is the
arithmetic that the narration is forbidden from doing itself.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import postrace


def _splits(paces, dist_m=1000.0):
    return [{"dist_m": dist_m, "dur_s": p * 60.0, "pace_min_km": p} for p in paces]


def _series(paces_per_km, *, hr=None, elev_per_km=None, per_km=10):
    """A per-point series built from a per-kilometre pace list."""
    pts = []
    for km, pace in enumerate(paces_per_km):
        for i in range(per_km):
            d = km + i / per_km
            e = None
            if elev_per_km is not None:
                e = elev_per_km[km]
            pts.append({"d": round(d, 2), "p": pace, "e": e,
                        "hr": (hr[km] if hr else None)})
    return pts


# ---------- the pace curve ----------

def test_curve_comes_from_the_watch_laps_when_they_are_kilometres():
    d = postrace.build_debrief(splits=_splits([5.0, 5.1, 5.0, 5.2, 5.1]),
                               dist_km=5.0, dur_min=25.4)
    assert d["source"] == "splits"
    assert [r["km"] for r in d["km_curve"]] == [1, 2, 3, 4, 5]


def test_curve_falls_back_to_the_series_when_auto_lap_was_off():
    """An AC: a race recorded as ONE lap still gets a curve, rebuilt from the track."""
    one_lap = [{"dist_m": 10000.0, "dur_s": 3000.0, "pace_min_km": 5.0}]
    d = postrace.build_debrief(splits=one_lap,
                               series=_series([5.0, 5.1, 5.0, 5.2, 5.1, 5.3]),
                               dist_km=6.0, dur_min=30.7)
    assert d["source"] == "series"
    assert len(d["km_curve"]) >= postrace.MIN_KM


def test_no_splits_and_no_series_still_produces_aggregates():
    """The last degradation step: no curve at all, but the debrief (and its narration)
    must still happen rather than crash."""
    d = postrace.build_debrief(splits=None, series=None, dist_km=21.1, dur_min=105.0,
                               avg_hr=165)
    assert "km_curve" not in d and d["source"] is None
    assert d["avg_pace_min_km"] == round(105.0 / 21.1, 2)
    assert d["avg_hr"] == 165
    assert "Дистанція" in postrace.summary(d)


# ---------- splits, fade, decoupling ----------

def test_positive_and_negative_splits():
    slowed = postrace.build_debrief(splits=_splits([4.8, 4.8, 5.2, 5.3]))
    assert slowed["halves"]["negative"] is False
    assert slowed["halves"]["delta_pct"] > 0

    surged = postrace.build_debrief(splits=_splits([5.2, 5.2, 4.9, 4.8]))
    assert surged["halves"]["negative"] is True
    assert surged["halves"]["delta_pct"] < 0


def test_fade_point_is_the_km_after_which_the_pace_never_returns():
    d = postrace.build_debrief(splits=_splits([5.0, 5.0, 5.0, 5.0, 5.6, 5.8, 6.0, 6.1]))
    assert d["fade_km"] == 5


def test_a_single_slow_km_recovered_from_is_not_a_fade():
    d = postrace.build_debrief(splits=_splits([5.0, 5.0, 5.6, 5.0, 5.0, 5.0]))
    assert d["fade_km"] is None


def test_split_and_fade_are_judged_on_gap_pace_not_raw():
    """The ticket's core gate: on a hilly course, raw splits describe the hills. The second
    half here climbs steeply and the raw pace drops accordingly — GAP-adjusted, the effort
    held, so this must NOT read as a fade."""
    paces = [5.0, 5.0, 5.0, 5.0, 5.8, 6.0, 6.1, 6.2]
    flat_then_climb = [100, 100, 100, 100, 140, 180, 220, 260]   # +40 m per km
    d = postrace.build_debrief(
        splits=_splits(paces),
        series=_series(paces, elev_per_km=flat_then_climb),
        dist_km=8.0, dur_min=44.0)
    gaps = [r["gap_pace_min_km"] for r in d["km_curve"]]
    assert gaps[-1] < d["km_curve"][-1]["pace_min_km"], "the climb must be credited"
    assert d["fade_km"] is None, "a hill is not a fade"

    # The same raw curve on a FLAT course is a genuine fade.
    flat = postrace.build_debrief(
        splits=_splits(paces), series=_series(paces, elev_per_km=[100] * 8),
        dist_km=8.0, dur_min=44.0)
    assert flat["fade_km"] == 5


def test_heart_rate_decoupling():
    """Speed per beat falls away in the second half → positive decoupling."""
    d = postrace.build_debrief(
        splits=_splits([5.0] * 8),
        series=_series([5.0] * 8, hr=[150, 150, 152, 152, 165, 168, 170, 172]))
    assert d["decoupling_pct"] > postrace.DECOUPLING_NOTE_PCT


def test_decoupling_is_none_without_heart_rate():
    d = postrace.build_debrief(splits=_splits([5.0] * 8), series=_series([5.0] * 8))
    assert "decoupling_pct" not in d


# ---------- against the schedule ----------

def test_no_target_means_no_schedule_section():
    """An AC: without a target time the section is ABSENT, not zero-filled."""
    d = postrace.build_debrief(splits=_splits([5.0] * 6), target_pace_min_km=None)
    assert "target" not in d
    assert "розкладки" not in postrace.summary(d)


def test_target_comparison_counts_kilometres_on_pace():
    d = postrace.build_debrief(splits=_splits([5.0, 5.0, 5.1, 5.4, 5.5, 5.6]),
                               target_pace_min_km=5.0)
    t = d["target"]
    assert t["target_pace_min_km"] == 5.0
    assert t["delta_s_per_km"] > 0            # slower than planned overall
    assert t["km_on_target"] == 2             # only the two 5:00 kms are within 5 s
    assert len(t["per_km"]) == 6


def test_target_pace_comes_from_the_structured_intake_not_prose():
    plan = SimpleNamespace(goal="first_10k", intake={"target_time_s": 50 * 60})
    assert postrace.target_pace_for_plan(plan, 10.0) == pytest.approx(5.0)
    assert postrace.target_pace_for_plan(
        SimpleNamespace(goal="first_10k", intake={}), 10.0) is None
    assert postrace.target_pace_for_plan(None, 10.0) is None


# ---------- delivery ----------

@pytest.mark.asyncio
async def test_debrief_is_one_claude_call_and_a_repeat_is_a_cache_hit(session):
    """An AC: exactly one call per race; ``/race done`` on the same activity again is a
    dedup-cache hit because the activity id is part of the key."""
    from app.analysis import reports
    from app.db.models import ActivityRecord, User

    user = User(email="race@example.com", password_hash="x", is_active=True)
    session.add(user)
    await session.flush()
    act = ActivityRecord(user_id=user.id, activity_id=777, date="2026-08-01", type="running",
                         dur_min=50.0, dist_km=10.0, avg_hr=168,
                         series=_series([5.0] * 10, hr=[160] * 10))
    session.add(act)
    await session.flush()

    calls = []

    def _fake(context, api_key=None):
        calls.append(context)
        from app.analysis.client import CallStats
        return "🏁 розбір", CallStats(kind="race_debrief", model="claude-sonnet-5")

    with patch.object(reports, "race_debrief_with_stats", _fake), \
            patch("app.garmin.client.fetch_activity_splits",
                  return_value=_splits([5.0] * 10)):
        first = await reports.run_race_debrief(
            session, user_id=user.id, activity=act, plan=None, api_key="k")
        second = await reports.run_race_debrief(
            session, user_id=user.id, activity=act, plan=None, api_key="k")

    assert first == second == "🏁 розбір"
    assert len(calls) == 1, "the second debrief of the same race must be a cache hit"
    assert act.analysis == "🏁 розбір"


@pytest.mark.asyncio
async def test_race_activity_is_found_only_by_explicit_evidence(session):
    """The ticket's risk note: never a "fast and long" heuristic. Only a session the plan
    marked as the race, or a run on the target date, counts."""
    from app.db.models import ActivityRecord, PlannedWorkout, TrainingPlan, User
    from bot import jobs as jobs_mod

    user = User(email="finder@example.com", password_hash="x", is_active=True,
                telegram_chat_id=42)
    session.add(user)
    await session.flush()
    plan = TrainingPlan(user_id=user.id, goal="first_10k", target_date="2026-08-01",
                        status="active")
    session.add(plan)
    await session.flush()
    session.add(PlannedWorkout(plan_id=plan.id, date="2026-08-01", type="race",
                               dist_km=10.0))
    # A hard, long training run three days earlier must NOT be taken for the race.
    session.add(ActivityRecord(user_id=user.id, activity_id=1, date="2026-07-29",
                               type="running", dist_km=21.0, dur_min=100.0))
    # Race day: a shakeout plus the race itself — the longer one wins.
    session.add(ActivityRecord(user_id=user.id, activity_id=2, date="2026-08-01",
                               type="running", dist_km=2.0, dur_min=12.0))
    session.add(ActivityRecord(user_id=user.id, activity_id=3, date="2026-08-01",
                               type="running", dist_km=10.0, dur_min=48.0))
    await session.flush()

    found = await jobs_mod.find_race_activity(session, user, plan)
    assert found.activity_id == 3


@pytest.mark.asyncio
async def test_archiving_a_plan_cancels_an_unsent_debrief(session):
    """An AC: a plan the runner archived must not produce a race debrief days later."""
    from app import race
    from app.db.models import TrainingPlan, User
    from app.garmin import repository

    user = User(email="archive@example.com", password_hash="x", is_active=True)
    session.add(user)
    await session.flush()
    plan = TrainingPlan(user_id=user.id, goal="first_10k", target_date="2026-08-01",
                        status="active")
    session.add(plan)
    await session.flush()

    await repository.archive_plan(session, plan)
    assert plan.status == "archived"
    assert await repository.get_state(
        session, user.id, race.stage_guard_key(plan.id, "debrief")) == "1"


@pytest.mark.asyncio
async def test_debrief_stage_does_not_burn_its_guard_before_the_run_syncs(session):
    """A race whose activity hasn't synced yet must leave the guard alone, so the next tick
    inside the catch-up window tries again instead of losing the debrief entirely."""
    from app import race
    from app.db.models import TrainingPlan, User
    from app.garmin import repository
    from bot import jobs as jobs_mod

    user = User(email="latesync@example.com", password_hash="x", is_active=True,
                telegram_chat_id=99)
    session.add(user)
    await session.flush()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    plan = TrainingPlan(user_id=user.id, goal="first_10k", target_date=yesterday,
                        status="active")
    session.add(plan)
    await session.flush()

    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    guard = race.stage_guard_key(plan.id, "debrief")
    await jobs_mod._send_race_debrief_stage(ctx, session, user, plan, guard)

    ctx.bot.send_message.assert_not_awaited()
    assert await repository.get_state(session, user.id, guard) is None
