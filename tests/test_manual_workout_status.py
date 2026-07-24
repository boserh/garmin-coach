"""ST-21: manual plan/actual status control.

A user can override a past session's status (done/skipped), unlink a wrong match and link a
specific activity; the auto-matcher must never overwrite a manual correction, and everything
is user-scoped."""
import datetime as dt

from app.db.models import (
    ActivityRecord,
    PlannedWorkout,
    TrainingPlan,
    User,
    WorkoutStatus,
)
from app.garmin import matching, repository

TODAY = dt.date.today()
YESTERDAY = (TODAY - dt.timedelta(days=1)).isoformat()
DAY_BEFORE = (TODAY - dt.timedelta(days=2)).isoformat()


async def _seed_user(session, email="u@e.com") -> int:
    user = User(email=email, password_hash="h")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user.id


async def _plan(session, uid):
    plan = TrainingPlan(user_id=uid, goal="first_5k", start_date=DAY_BEFORE,
                        target_date=(TODAY + dt.timedelta(weeks=8)).isoformat(),
                        status="active")
    session.add(plan)
    await session.flush()
    return plan


async def _workout(session, plan, uid, date, *, type_="easy", dist_km=5.0, status="planned"):
    w = PlannedWorkout(plan_id=plan.id, user_id=uid, date=date, week=1,
                       type=type_, dist_km=dist_km, status=status)
    session.add(w)
    await session.flush()
    return w


async def _activity(session, uid, aid, date, *, dist_km=5.0, type_="running"):
    a = ActivityRecord(user_id=uid, activity_id=aid, date=date, type=type_,
                       dist_km=dist_km, dur_min=25.0)
    session.add(a)
    await session.flush()
    return a


async def test_manual_done_marks_and_tags(session):
    uid = await _seed_user(session)
    plan = await _plan(session, uid)
    w = await _workout(session, plan, uid, YESTERDAY, status="missed")
    await session.commit()

    out = await repository.set_workout_status(session, uid, w.id, "done")
    await session.commit()
    assert out is not None
    await session.refresh(w)
    assert w.status == WorkoutStatus.DONE
    assert w.match_info.get("manual") is True


async def test_manual_skipped_unlinks(session):
    uid = await _seed_user(session)
    plan = await _plan(session, uid)
    a = await _activity(session, uid, 1, YESTERDAY)
    w = await _workout(session, plan, uid, YESTERDAY)
    w.completed_activity_id = a.id
    w.status = WorkoutStatus.DONE
    await session.commit()

    await repository.set_workout_status(session, uid, w.id, "skipped")
    await session.commit()
    await session.refresh(w)
    assert w.status == WorkoutStatus.SKIPPED
    assert w.completed_activity_id is None
    assert w.match_info.get("manual") is True


async def test_unlink_frees_activity_and_resets_status(session):
    uid = await _seed_user(session)
    plan = await _plan(session, uid)
    a = await _activity(session, uid, 1, YESTERDAY)
    w = await _workout(session, plan, uid, YESTERDAY)
    w.completed_activity_id = a.id
    w.status = WorkoutStatus.DONE
    w.match_info = {"actual_dist_km": 5.0}
    await session.commit()

    await repository.set_workout_status(session, uid, w.id, "unlink")
    await session.commit()
    await session.refresh(w)
    assert w.completed_activity_id is None
    assert w.match_info is None
    assert w.status == WorkoutStatus.MISSED   # past date


async def test_manual_link_attaches_candidate(session):
    uid = await _seed_user(session)
    plan = await _plan(session, uid)
    a = await _activity(session, uid, 1, YESTERDAY, dist_km=6.0)
    w = await _workout(session, plan, uid, YESTERDAY, status="missed")
    await session.commit()

    cands = await repository.link_candidates(session, uid, w)
    assert a.id in [c.id for c in cands]

    out = await repository.set_workout_status(session, uid, w.id, "link", activity_id=a.id)
    await session.commit()
    assert out is not None
    await session.refresh(w)
    assert w.status == WorkoutStatus.DONE
    assert w.completed_activity_id == a.id
    assert w.match_info.get("manual") is True
    assert w.match_info.get("actual_dist_km") == 6.0


async def test_link_rejects_incompatible_or_far_activity(session):
    uid = await _seed_user(session)
    plan = await _plan(session, uid)
    # A cycling activity 3 days off — not a candidate for a running session.
    far = await _activity(session, uid, 1, DAY_BEFORE, type_="cycling")
    w = await _workout(session, plan, uid, TODAY.isoformat(), status="planned")
    await session.commit()
    cands = await repository.link_candidates(session, uid, w)
    assert far.id not in [c.id for c in cands]
    # Linking it anyway is refused.
    assert await repository.set_workout_status(
        session, uid, w.id, "link", activity_id=far.id) is None


async def test_matcher_never_overwrites_manual(session):
    uid = await _seed_user(session)
    plan = await _plan(session, uid)
    # Manually marked missed, tagged manual, but left in a state the matcher could touch.
    w = await _workout(session, plan, uid, YESTERDAY, status="planned")
    w.match_info = {"manual": True}
    w.status = WorkoutStatus.PLANNED
    # An activity exists that would otherwise match.
    await _activity(session, uid, 1, YESTERDAY, dist_km=5.0)
    await session.commit()

    result = await matching.match_activities(session, uid)
    assert result["done"] == 0
    await session.refresh(w)
    assert w.completed_activity_id is None   # untouched


async def test_status_cross_user_isolation(session):
    uid = await _seed_user(session)
    other = await _seed_user(session, email="o@e.com")
    plan = await _plan(session, uid)
    w = await _workout(session, plan, uid, YESTERDAY)
    await session.commit()
    assert await repository.set_workout_status(session, other, w.id, "done") is None


async def test_manual_status_idempotent(session):
    uid = await _seed_user(session)
    plan = await _plan(session, uid)
    w = await _workout(session, plan, uid, YESTERDAY, status="missed")
    await session.commit()
    await repository.set_workout_status(session, uid, w.id, "done")
    await session.commit()
    await repository.set_workout_status(session, uid, w.id, "done")
    await session.commit()
    await session.refresh(w)
    assert w.status == WorkoutStatus.DONE
    assert w.match_info.get("manual") is True
