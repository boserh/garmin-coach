"""Plan/actual matching: unit tests for matching.match_activities."""
import datetime as dt

import pytest

from app.db.models import ActivityRecord, PlannedWorkout, TrainingPlan, WorkoutStatus
from app.garmin import matching

U1 = 1
TODAY = dt.date.today().isoformat()
YESTERDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()
DAY_BEFORE = (dt.date.today() - dt.timedelta(days=2)).isoformat()


# ---- helpers ---------------------------------------------------------------

async def _make_plan(session, start=None, target=None):
    start = start or YESTERDAY
    target = target or (dt.date.today() + dt.timedelta(weeks=8)).isoformat()
    plan = TrainingPlan(
        user_id=U1, goal="first_5k", goal_label="Перші 5 км",
        start_date=start, target_date=target, status="active",
    )
    session.add(plan)
    await session.flush()
    return plan


async def _make_workout(session, plan, date, *, type_="easy", dist_km=5.0, status="planned"):
    w = PlannedWorkout(
        plan_id=plan.id, user_id=U1, date=date, week=1,
        type=type_, dist_km=dist_km, description="", status=status,
    )
    session.add(w)
    await session.flush()
    return w


async def _make_activity(session, date, *, dist_km=5.0, dur_min=30.0, type_="running"):
    a = ActivityRecord(
        user_id=U1, activity_id=hash(f"{date}{dist_km}{type_}") & 0xFFFFFFF,
        date=date, type=type_, dist_km=dist_km, dur_min=dur_min,
    )
    session.add(a)
    await session.flush()
    return a


# ---- tests -----------------------------------------------------------------

async def test_exact_date_match_sets_done(session):
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, YESTERDAY, dist_km=5.0)
    a = await _make_activity(session, YESTERDAY, dist_km=5.0)
    await session.commit()

    result = await matching.match_activities(session, U1)

    assert result["done"] == 1
    assert result["partial"] == 0
    assert result["missed"] == 0
    await session.refresh(w)
    assert w.status == WorkoutStatus.DONE
    assert w.completed_activity_id == a.id
    assert w.match_info["dist_delta_km"] == pytest.approx(0.0, abs=0.01)


async def test_adjacent_day_match(session):
    """An activity the day after the workout date (±1 day tolerance)."""
    plan = await _make_plan(session, start=DAY_BEFORE)
    w = await _make_workout(session, plan, DAY_BEFORE, dist_km=6.0)
    # activity is on YESTERDAY (one day after DAY_BEFORE) — within ±1 day
    a = await _make_activity(session, YESTERDAY, dist_km=6.0)
    await session.commit()

    result = await matching.match_activities(session, U1)

    assert result["done"] == 1
    await session.refresh(w)
    assert w.status == WorkoutStatus.DONE
    assert w.completed_activity_id == a.id


async def test_two_activities_same_day_picks_closest_distance(session):
    """When two activities fall on the same day, the one closest in distance wins."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, YESTERDAY, dist_km=10.0)
    await _make_activity(session, YESTERDAY, dist_km=5.0, type_="trail_running")
    long_ = await _make_activity(session, YESTERDAY, dist_km=10.2, type_="running")
    await session.commit()

    await matching.match_activities(session, U1)

    await session.refresh(w)
    assert w.completed_activity_id == long_.id  # 10.2 km closer to 10.0 than 5.0


async def test_distance_over_threshold_gives_partial(session):
    """Activity distance deviates > 25% from plan → partial."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, YESTERDAY, dist_km=10.0)
    # 10 * 1.26 = 12.6 km — well over 25% threshold
    a = await _make_activity(session, YESTERDAY, dist_km=12.6)
    await session.commit()

    result = await matching.match_activities(session, U1)

    assert result["partial"] == 1
    await session.refresh(w)
    assert w.status == WorkoutStatus.PARTIAL
    assert w.completed_activity_id == a.id


async def test_no_activity_before_yesterday_stays_planned(session):
    """A workout dated TODAY with no activity must NOT become missed (may still sync)."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, TODAY, dist_km=5.0)
    await session.commit()

    result = await matching.match_activities(session, U1)

    assert result["missed"] == 0
    await session.refresh(w)
    assert w.status == WorkoutStatus.PLANNED  # unchanged


async def test_no_activity_yesterday_becomes_missed(session):
    """A workout dated yesterday with no matching activity → missed."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, YESTERDAY, dist_km=5.0)
    await session.commit()

    result = await matching.match_activities(session, U1)

    assert result["missed"] == 1
    await session.refresh(w)
    assert w.status == WorkoutStatus.MISSED


async def test_idempotent_already_done_not_touched(session):
    """A workout already marked done is ignored on a second pass."""
    plan = await _make_plan(session)
    a = await _make_activity(session, YESTERDAY, dist_km=5.0)
    w = await _make_workout(session, plan, YESTERDAY, dist_km=5.0, status="done")
    w.completed_activity_id = a.id
    await session.commit()

    result = await matching.match_activities(session, U1)

    # Nothing new to process — done workout is skipped
    assert result == {"done": 0, "partial": 0, "missed": 0}
    await session.refresh(w)
    assert w.status == WorkoutStatus.DONE  # unchanged


async def test_skipped_not_overwritten(session):
    """A manually skipped workout must not be converted to missed."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, YESTERDAY, dist_km=5.0, status="skipped")
    await session.commit()

    await matching.match_activities(session, U1)

    await session.refresh(w)
    assert w.status == WorkoutStatus.SKIPPED  # untouched


async def test_rest_type_not_matched(session):
    """Rest days are never matched regardless of activity presence."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, YESTERDAY, type_="rest", dist_km=None)
    await _make_activity(session, YESTERDAY, dist_km=5.0)
    await session.commit()

    result = await matching.match_activities(session, U1)

    assert result == {"done": 0, "partial": 0, "missed": 0}
    await session.refresh(w)
    assert w.status == WorkoutStatus.PLANNED


async def test_strength_not_matched_by_running_activity(session):
    """A strength day is not satisfied by a *running* activity (today's stays planned)."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, TODAY, type_="strength", dist_km=None)
    await _make_activity(session, TODAY, dist_km=5.0, type_="running")
    await session.commit()

    await matching.match_activities(session, U1)

    await session.refresh(w)
    assert w.status == WorkoutStatus.PLANNED
    assert w.completed_activity_id is None


async def test_strength_matched_by_strength_activity(session):
    """A strength day is marked done by a strength_training activity within ±1 day."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, YESTERDAY, type_="strength", dist_km=None)
    a = await _make_activity(
        session, YESTERDAY, dist_km=0.0, dur_min=45.0, type_="strength_training")
    await session.commit()

    result = await matching.match_activities(session, U1)

    assert result["done"] == 1
    await session.refresh(w)
    assert w.status == WorkoutStatus.DONE
    assert w.completed_activity_id == a.id


async def test_strength_missed_when_no_activity(session):
    """A past strength day with no strength activity → missed."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, YESTERDAY, type_="strength", dist_km=None)
    await session.commit()

    result = await matching.match_activities(session, U1)

    assert result["missed"] == 1
    await session.refresh(w)
    assert w.status == WorkoutStatus.MISSED


async def test_strength_today_stays_planned(session):
    """Today's strength day with no activity yet must not be marked missed."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, TODAY, type_="strength", dist_km=None)
    await session.commit()

    result = await matching.match_activities(session, U1)

    assert result["missed"] == 0
    await session.refresh(w)
    assert w.status == WorkoutStatus.PLANNED


async def test_no_active_plan_returns_zeros(session):
    result = await matching.match_activities(session, U1)
    assert result == {"done": 0, "partial": 0, "missed": 0}


async def test_activity_not_linked_to_two_workouts(session):
    """One activity must satisfy at most one workout (used_ids guard)."""
    plan = await _make_plan(session, start=DAY_BEFORE)
    w1 = await _make_workout(session, plan, DAY_BEFORE, dist_km=5.0)
    w2 = await _make_workout(session, plan, YESTERDAY, dist_km=5.0)
    # Only one activity — on DAY_BEFORE
    a = await _make_activity(session, DAY_BEFORE, dist_km=5.0)
    await session.commit()

    result = await matching.match_activities(session, U1)

    await session.refresh(w1)
    await session.refresh(w2)
    # w1 grabs the activity; w2 becomes missed (yesterday, no match left)
    assert w1.completed_activity_id == a.id
    assert w2.status == WorkoutStatus.MISSED
    assert result["done"] == 1
    assert result["missed"] == 1


async def test_match_info_contains_pace(session):
    """match_info should include actual_pace_minkm when dur_min is available."""
    plan = await _make_plan(session)
    w = await _make_workout(session, plan, YESTERDAY, dist_km=5.0)
    # 30 min / 5 km = 6.0 min/km
    await _make_activity(session, YESTERDAY, dist_km=5.0, dur_min=30.0)
    await session.commit()

    await matching.match_activities(session, U1)

    await session.refresh(w)
    assert w.match_info is not None
    assert w.match_info["actual_pace_minkm"] == pytest.approx(6.0, abs=0.01)
