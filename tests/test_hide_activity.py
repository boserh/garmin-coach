"""ST-17: hide an activity (dup / broken track).

A hidden row must drop out of every list / aggregate / record / plan-match, must survive
the next Garmin sync (``upsert_activity`` never resets the flag), and hiding must clean up
the poisoned downstream state (a fake PB, a wrong plan match)."""
import datetime as dt

from sqlalchemy import func, select

from app import records
from app.db.models import (
    ActivityRecord,
    PersonalRecord,
    PlannedWorkout,
    TrainingPlan,
    User,
    WorkoutStatus,
)
from app.garmin import matching, repository

TODAY = dt.date.today()
YESTERDAY = (TODAY - dt.timedelta(days=1)).isoformat()


async def _seed_user(session, email="u@e.com") -> int:
    user = User(email=email, password_hash="h")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user.id


async def _activity(session, uid, aid, date, *, dist_km=5.0, dur_min=25.0, type_="running"):
    a = ActivityRecord(user_id=uid, activity_id=aid, date=date, type=type_,
                       dist_km=dist_km, dur_min=dur_min, avg_hr=150)
    session.add(a)
    await session.flush()
    return a


async def test_hidden_excluded_from_lists_and_aggregates(session):
    uid = await _seed_user(session)
    visible = await _activity(session, uid, 1, YESTERDAY, dist_km=5.0)
    hidden = await _activity(session, uid, 2, YESTERDAY, dist_km=5.0)
    await session.commit()

    await repository.set_activity_hidden(session, uid, hidden.id, True)
    await session.commit()

    lst = await repository.list_activities(session, uid, n=10)
    assert [r["id"] for r in lst] == [visible.id]

    q = await repository.query_activities(session, uid)
    assert [r["id"] for r in q] == [visible.id]

    vol = await repository.weekly_run_volume(session, uid, weeks=4)
    assert sum(b["runs"] for b in vol) == 1   # only the visible run counts

    last = await repository.get_last_activity(session, uid)
    assert last.id == visible.id


async def test_hide_deletes_records_and_ignores_in_detection(session):
    uid = await _seed_user(session)
    # A single fast 5K → a fastest_5k + longest_run records.
    fast = await _activity(session, uid, 1, YESTERDAY, dist_km=5.0, dur_min=20.0)
    await session.commit()
    inserted = await records.detect_records(session, uid)
    await session.commit()
    assert any(r.kind == "fastest_5k" for r in inserted)
    # A record row points at this activity.
    n_pr = (await session.execute(
        select(func.count()).select_from(PersonalRecord).where(
            PersonalRecord.activity_id == fast.id))).scalar_one()
    assert n_pr >= 1

    await repository.set_activity_hidden(session, uid, fast.id, True)
    await session.commit()

    # Its PersonalRecord rows are gone …
    n_pr = (await session.execute(
        select(func.count()).select_from(PersonalRecord).where(
            PersonalRecord.activity_id == fast.id))).scalar_one()
    assert n_pr == 0
    # … and a fresh detection over only the hidden activity produces nothing.
    again = await records.detect_records(session, uid)
    assert not any(r.kind == "fastest_5k" for r in again)


async def test_hide_unmatches_planned_workout(session):
    uid = await _seed_user(session)
    plan = TrainingPlan(user_id=uid, goal="first_5k", start_date=YESTERDAY,
                        target_date=(TODAY + dt.timedelta(weeks=8)).isoformat(),
                        status="active")
    session.add(plan)
    await session.flush()
    w = PlannedWorkout(plan_id=plan.id, user_id=uid, date=YESTERDAY, week=1,
                       type="easy", dist_km=5.0, status="planned")
    session.add(w)
    a = await _activity(session, uid, 1, YESTERDAY, dist_km=5.0)
    await session.commit()

    result = await matching.match_activities(session, uid)
    assert result["done"] == 1
    await session.refresh(w)
    assert w.completed_activity_id == a.id

    await repository.set_activity_hidden(session, uid, a.id, True)
    await session.commit()
    await session.refresh(w)
    assert w.completed_activity_id is None
    assert w.match_info is None
    assert w.status == WorkoutStatus.MISSED   # past date with no match

    # The hidden activity is no longer a match candidate.
    result2 = await matching.match_activities(session, uid)
    assert result2["done"] == 0


async def test_upsert_does_not_reset_hidden(session):
    uid = await _seed_user(session)
    a = await _activity(session, uid, 1, YESTERDAY, dist_km=3.0)
    await session.commit()
    await repository.set_activity_hidden(session, uid, a.id, True)
    await session.commit()

    # A later sync updates the row's numbers — but must not un-hide it.
    await repository.upsert_activity(session, uid, 1, {
        "date": YESTERDAY, "type": "running", "dur_min": 30.0, "dist_km": 5.0,
        "avg_hr": 150, "max_hr": 160, "load": 50.0})
    await session.commit()
    await session.refresh(a)
    assert a.is_hidden is True
    assert a.dist_km == 5.0   # fields still updated


async def test_unhide_restores_visibility(session):
    uid = await _seed_user(session)
    a = await _activity(session, uid, 1, YESTERDAY, dist_km=5.0)
    await session.commit()
    await repository.set_activity_hidden(session, uid, a.id, True)
    await session.commit()
    assert await repository.list_activities(session, uid) == []
    await repository.set_activity_hidden(session, uid, a.id, False)
    await session.commit()
    assert len(await repository.list_activities(session, uid)) == 1


async def test_hide_cross_user_isolation(session):
    uid = await _seed_user(session)
    other = await _seed_user(session, email="o@e.com")
    a = await _activity(session, uid, 1, YESTERDAY)
    await session.commit()
    # The other user can't hide someone else's activity.
    assert await repository.set_activity_hidden(session, other, a.id, True) is None
    await session.refresh(a)
    assert a.is_hidden is False
