"""OPS-09: /plan sync-status badges, last-sync summary block, and POST /plan/sync
(manual "Синхронізувати зараз" button) — the web surface over plan_sync.sync_plan_to_garmin."""
import datetime as dt
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import anyio

from app.db.base import async_session_maker
from app.db.models import PlannedWorkout, TrainingPlan, User
from app.garmin import repository
from tests.web_helpers import _user_id


@asynccontextmanager
async def _fake_runtime(session, user):
    """Skip the real Garmin provider construction (needs live creds) — sync_plan_to_garmin
    itself is mocked in these tests, only the surrounding user_runtime context matters."""
    yield None


def _seed_plan_with_workout(user_id: int, **workout_kw):
    async def seed():
        async with async_session_maker() as s:
            plan = TrainingPlan(user_id=user_id, goal="general", status="active",
                                 start_date="2026-07-01", days_per_week=3, intensity="moderate")
            s.add(plan)
            await s.flush()
            s.add(PlannedWorkout(plan_id=plan.id, user_id=user_id, **workout_kw))
            await s.commit()
            return plan.id

    return anyio.run(seed)


def _set_sync_enabled(user_id: int, enabled: bool):
    async def go():
        async with async_session_maker() as s:
            u = await s.get(User, user_id)
            u.garmin_sync_enabled = enabled
            await s.commit()

    anyio.run(go)


def test_plan_page_shows_pending_badge_for_unpushed_upcoming_session(auth_client):
    uid = _user_id("t@example.com")
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    _seed_plan_with_workout(uid, date=fut, week=1, type="easy", dist_km=5.0, status="planned")
    body = auth_client.get("/plan").text
    assert "не запушено" in body


def test_plan_page_shows_on_watch_badge_when_pushed(auth_client):
    uid = _user_id("t@example.com")
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    _seed_plan_with_workout(uid, date=fut, week=1, type="easy", dist_km=5.0, status="planned",
                             garmin_workout_id=111, garmin_schedule_id=222)
    body = auth_client.get("/plan").text
    assert "на годиннику" in body


def test_plan_sync_button_hidden_when_toggle_off(auth_client):
    uid = _user_id("t@example.com")
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    _seed_plan_with_workout(uid, date=fut, week=1, type="easy", dist_km=5.0, status="planned")
    _set_sync_enabled(uid, False)
    body = auth_client.get("/plan").text
    assert "Синхронізувати зараз" not in body


def test_plan_sync_post_skips_when_toggle_off(auth_client):
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")
    _set_sync_enabled(uid, False)
    plan_router._sync_guard.clear()
    with patch.object(plan_router, "user_runtime", _fake_runtime), \
         patch.object(plan_router.plan_sync, "sync_plan_to_garmin", AsyncMock()) as m:
        r = auth_client.post("/plan/sync", follow_redirects=False)
    assert r.status_code == 303
    m.assert_not_called()


def test_plan_sync_post_success_shows_ok(auth_client):
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")
    _set_sync_enabled(uid, True)
    plan_router._sync_guard.clear()
    with patch.object(plan_router, "user_runtime", _fake_runtime), \
         patch.object(plan_router.plan_sync, "sync_plan_to_garmin",
                       AsyncMock(return_value={"pushed": 1, "removed": 0, "errors": []})) as m:
        r = auth_client.post("/plan/sync", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/plan?synced=ok"
    m.assert_called_once()


def test_plan_sync_post_reports_errors(auth_client):
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")
    _set_sync_enabled(uid, True)
    plan_router._sync_guard.clear()
    errors = [{"workout_id": 9, "step": "push", "msg": "boom"}]
    with patch.object(plan_router, "user_runtime", _fake_runtime), \
         patch.object(plan_router.plan_sync, "sync_plan_to_garmin",
                       AsyncMock(return_value={"pushed": 0, "removed": 0, "errors": errors})):
        r = auth_client.post("/plan/sync", follow_redirects=False)
    assert r.headers["location"] == "/plan?synced=err"


def test_plan_sync_post_rate_limited_on_double_tap(auth_client):
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")
    _set_sync_enabled(uid, True)
    plan_router._sync_guard.clear()
    with patch.object(plan_router, "user_runtime", _fake_runtime), \
         patch.object(plan_router.plan_sync, "sync_plan_to_garmin",
                       AsyncMock(return_value={"pushed": 0, "removed": 0, "errors": []})) as m:
        auth_client.post("/plan/sync", follow_redirects=False)
        r2 = auth_client.post("/plan/sync", follow_redirects=False)
    assert r2.headers["location"] == "/plan?synced=wait"
    assert m.call_count == 1


def test_plan_page_shows_last_sync_summary(auth_client):
    uid = _user_id("t@example.com")
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    plan_id = _seed_plan_with_workout(uid, date=fut, week=1, type="easy", status="planned")

    async def seed_summary():
        async with async_session_maker() as s:
            await repository.set_plan_sync_summary(s, uid, plan_id, 2, 1, [])

    anyio.run(seed_summary)
    body = auth_client.get("/plan").text
    assert "Останній синк" in body
    assert "+2 / -1" in body
