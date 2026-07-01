"""Garmin calendar sync: forward push + cleanup sweep (client/provider mocked)."""
import datetime as dt
from unittest.mock import Mock, patch

from app.db.models import PlannedWorkout, TrainingPlan
from app.garmin import plan_sync, repository

U1 = 1


async def _seed_plan(session, *, status="active", workouts):
    plan = TrainingPlan(user_id=U1, goal="g", status=status)
    session.add(plan)
    await session.flush()
    for w in workouts:
        session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, **w))
    await session.commit()
    return plan


def _prov():
    p = Mock()
    p.login = Mock()
    return p


async def test_sync_pushes_upcoming_and_stores_ids(session):
    fut = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    plan = await _seed_plan(session, workouts=[
        dict(date=fut, week=1, type="easy", dist_km=5.0, status="planned"),
    ])
    with patch.object(plan_sync, "get_provider", return_value=_prov()), \
         patch.object(plan_sync.client, "create_workout", return_value={"workoutId": 111}), \
         patch.object(plan_sync.client, "schedule_workout",
                      return_value={"workoutScheduleId": 222}):
        res = await plan_sync.sync_plan_to_garmin(session, U1, days=14)
    assert res == {"pushed": 1, "removed": 0}
    ws = await repository.list_workouts(session, plan.id)
    assert ws[0].garmin_workout_id == 111 and ws[0].garmin_schedule_id == 222


async def test_sync_skips_rest_and_already_pushed(session):
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    fut2 = (dt.date.today() + dt.timedelta(days=4)).isoformat()
    await _seed_plan(session, workouts=[
        dict(date=fut, week=1, type="rest", status="planned"),                  # not a run
        dict(date=fut2, week=1, type="easy", status="planned",
             garmin_workout_id=5, garmin_schedule_id=6),                        # already pushed
    ])
    with patch.object(plan_sync, "get_provider", return_value=_prov()), \
         patch.object(plan_sync.client, "create_workout") as create:
        res = await plan_sync.sync_plan_to_garmin(session, U1, days=14)
    assert res == {"pushed": 0, "removed": 0}
    create.assert_not_called()


async def test_sync_removes_pushed_from_archived_plan(session):
    fut = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    await _seed_plan(session, status="archived", workouts=[
        dict(date=fut, week=1, type="easy", status="planned",
             garmin_workout_id=999, garmin_schedule_id=888),
    ])
    with patch.object(plan_sync, "get_provider", return_value=_prov()), \
         patch.object(plan_sync.client, "delete_workout") as dele:
        res = await plan_sync.sync_plan_to_garmin(session, U1, days=14)
    assert res == {"pushed": 0, "removed": 1}
    dele.assert_called_once_with(999)
    assert await repository.list_pushed_workouts(session, U1) == []   # ids cleared


async def test_resync_moved_workout_redrops_and_repushes(session):
    fut = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    new = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    plan = await _seed_plan(session, workouts=[
        dict(date=fut, week=1, type="easy", dist_km=5.0, status="planned",
             garmin_workout_id=10, garmin_schedule_id=11),
    ])
    (w,) = await repository.list_workouts(session, plan.id)
    w.date = new  # simulate a `move` edit having changed the date
    with patch.object(plan_sync, "get_provider", return_value=_prov()), \
         patch.object(plan_sync.client, "delete_workout") as dele, \
         patch.object(plan_sync.client, "create_workout", return_value={"workoutId": 12}), \
         patch.object(plan_sync.client, "schedule_workout",
                      return_value={"workoutScheduleId": 13}):
        res = await plan_sync.resync_workouts(session, U1, [w])
    assert res == {"pushed": 1, "removed": 1}
    dele.assert_called_once_with(10)          # old copy dropped
    assert w.garmin_workout_id == 12          # re-pushed (on the new date)


async def test_resync_skipped_only_removes(session):
    fut = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    plan = await _seed_plan(session, workouts=[
        dict(date=fut, week=1, type="easy", status="skipped",
             garmin_workout_id=10, garmin_schedule_id=11),
    ])
    (w,) = await repository.list_workouts(session, plan.id)
    with patch.object(plan_sync, "get_provider", return_value=_prov()), \
         patch.object(plan_sync.client, "delete_workout") as dele, \
         patch.object(plan_sync.client, "create_workout") as create:
        res = await plan_sync.resync_workouts(session, U1, [w])
    assert res == {"pushed": 0, "removed": 1}
    dele.assert_called_once_with(10)
    create.assert_not_called()


async def test_sync_removes_past_and_pushes_future(session):
    past = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    fut = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    await _seed_plan(session, workouts=[
        dict(date=past, week=1, type="easy", status="planned",
             garmin_workout_id=999, garmin_schedule_id=888),
        dict(date=fut, week=1, type="easy", dist_km=5.0, status="planned"),
    ])
    with patch.object(plan_sync, "get_provider", return_value=_prov()), \
         patch.object(plan_sync.client, "delete_workout") as dele, \
         patch.object(plan_sync.client, "create_workout", return_value={"workoutId": 111}), \
         patch.object(plan_sync.client, "schedule_workout",
                      return_value={"workoutScheduleId": 222}):
        res = await plan_sync.sync_plan_to_garmin(session, U1, days=14)
    assert res == {"pushed": 1, "removed": 1}
    dele.assert_called_once_with(999)
