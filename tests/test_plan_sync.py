"""Garmin calendar sync: forward push + cleanup sweep (client/provider mocked)."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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


async def test_push_clones_strength_template_not_build(session):
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    await _seed_plan(session, workouts=[
        dict(date=fut, week=2, type="strength", status="planned",
             description="Day 1", garmin_template_id=931013083),
    ])
    with patch.object(plan_sync, "get_provider", return_value=_prov()), \
         patch.object(plan_sync.client, "fetch_workout_full",
                      return_value={"workoutSegments": [{"workoutSteps": []}]}) as fetch, \
         patch.object(plan_sync.client, "create_workout", return_value={"workoutId": 555}), \
         patch.object(plan_sync.client, "schedule_workout",
                      return_value={"workoutScheduleId": 556}), \
         patch.object(plan_sync.workout_export, "build_workout") as build:
        res = await plan_sync.sync_plan_to_garmin(session, U1, days=14)
    assert res == {"pushed": 1, "removed": 0}
    fetch.assert_called_once_with(931013083)   # cloned the template
    build.assert_not_called()                   # not built from steps
    (w,) = await repository.list_pushed_workouts(session, U1)
    assert w.garmin_workout_id == 555           # our own copy, not the template id


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


async def test_unpush_all_removes_every_pushed(session):
    fut = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    await _seed_plan(session, workouts=[
        dict(date=fut, week=1, type="easy", status="planned",
             garmin_workout_id=1, garmin_schedule_id=2),
    ])
    with patch.object(plan_sync, "get_provider", return_value=_prov()), \
         patch.object(plan_sync.client, "delete_workout") as dele:
        n = await plan_sync.unpush_all(session, U1)
    assert n == 1
    dele.assert_called_once_with(1)
    assert await repository.list_pushed_workouts(session, U1) == []


async def test_sync_for_user_skips_when_toggle_off(session):
    from bot import jobs
    user = SimpleNamespace(id=U1, garmin_sync_enabled=False)
    with patch.object(jobs.plan_sync, "sync_plan_to_garmin", new=AsyncMock()) as m:
        await jobs._sync_for_user(session, user)
    m.assert_not_called()


async def test_today_done_workout_stays_on_calendar(session):
    """A workout completed today (status=done) must not be removed until tomorrow."""
    today = dt.date.today().isoformat()
    await _seed_plan(session, workouts=[
        dict(date=today, week=1, type="easy", status="done",
             garmin_workout_id=777, garmin_schedule_id=666),
    ])
    with patch.object(plan_sync, "get_provider", return_value=_prov()), \
         patch.object(plan_sync.client, "delete_workout") as dele, \
         patch.object(plan_sync.client, "create_workout") as create:
        res = await plan_sync.sync_plan_to_garmin(session, U1, days=14)
    assert res == {"pushed": 0, "removed": 0}
    dele.assert_not_called()
    create.assert_not_called()
    # ids still set on the row
    (w,) = await repository.list_pushed_workouts(session, U1)
    assert w.garmin_workout_id == 777


async def test_select_forward_window_and_pushable(session):
    """The shared forward selection (CODE-02): in-window + pushable + not-yet-pushed."""
    near = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    far = (dt.date.today() + dt.timedelta(days=40)).isoformat()
    plan = await _seed_plan(session, workouts=[
        dict(date=near, week=1, type="easy", dist_km=5.0, status="planned"),      # in
        dict(date=near, week=1, type="rest", status="planned"),                   # not pushable
        dict(date=near, week=1, type="easy", status="planned",
             garmin_workout_id=9, garmin_schedule_id=8),                          # already pushed
        dict(date=far, week=6, type="easy", dist_km=5.0, status="planned"),       # out of window
    ])
    todo = await plan_sync.select_forward(session, plan.id, days=14)
    assert [w.date for w in todo] == [near]


async def test_select_forward_only_date(session):
    """--date narrows the selection to a single session, ignoring the window."""
    d1 = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    d2 = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    plan = await _seed_plan(session, workouts=[
        dict(date=d1, week=1, type="easy", dist_km=5.0, status="planned"),
        dict(date=d2, week=1, type="easy", dist_km=6.0, status="planned"),
    ])
    todo = await plan_sync.select_forward(session, plan.id, only_date=d2)
    assert [w.date for w in todo] == [d2]


async def test_select_pushed_and_only_date(session):
    """The shared remove selection (CODE-02): only rows WE pushed, optionally one date."""
    d1 = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    d2 = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    plan = await _seed_plan(session, workouts=[
        dict(date=d1, week=1, type="easy", status="planned",
             garmin_workout_id=1, garmin_schedule_id=2),
        dict(date=d2, week=1, type="easy", status="planned"),                     # never pushed
    ])
    assert [w.date for w in await plan_sync.select_pushed(session, plan.id)] == [d1]
    assert await plan_sync.select_pushed(session, plan.id, only_date=d2) == []


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
