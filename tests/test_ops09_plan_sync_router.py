"""OPS-09: /plan sync-status badges, last-sync summary block, and POST /plan/sync
(manual "Синхронізувати зараз" button) — the web surface over plan_sync.sync_plan_to_garmin."""
import datetime as dt
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

from app.core.crypto import hash_password
from app.db import users
from app.db.base import async_session_maker
from app.db.models import PlannedWorkout, TrainingPlan
from app.garmin import repository
from app.main import create_app


@asynccontextmanager
async def _fake_runtime(session, user):
    """Skip the real Garmin provider construction (needs live creds) — sync_plan_to_garmin
    itself is mocked in these tests, only the surrounding user_runtime context matters."""
    yield None


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def _seed_user(email, password="pw", garmin_sync_enabled=True):
    async def seed():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, email)
            if not u:
                u = await users.create_user(
                    s, email=email, password_hash=hash_password(password), is_admin=False,
                )
            u.garmin_sync_enabled = garmin_sync_enabled
            await s.commit()
            return u.id

    return anyio.run(seed)


@pytest.fixture
def auth_client(client, request):
    # A distinct email per test → a distinct user row → no plan/sync-guard bleed between
    # tests sharing the file-backed test DB (unlike the in-memory `session` fixture, this
    # one persists for the whole test run — see test_chat.py's identical pattern).
    email = f"{request.node.name}@example.com"
    uid = _seed_user(email)
    r = client.post("/login", data={"email": email, "password": "pw"})
    assert r.status_code == 200
    return client, uid


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
    from app.db.models import User

    async def go():
        async with async_session_maker() as s:
            u = await s.get(User, user_id)
            u.garmin_sync_enabled = enabled
            await s.commit()

    anyio.run(go)


def test_plan_page_shows_pending_badge_for_unpushed_upcoming_session(auth_client):
    client, uid = auth_client
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    _seed_plan_with_workout(uid, date=fut, week=1, type="easy", dist_km=5.0, status="planned")
    body = client.get("/plan").text
    assert "не запушено" in body


def test_plan_page_shows_on_watch_badge_when_pushed(auth_client):
    client, uid = auth_client
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    _seed_plan_with_workout(uid, date=fut, week=1, type="easy", dist_km=5.0, status="planned",
                             garmin_workout_id=111, garmin_schedule_id=222)
    body = client.get("/plan").text
    assert "на годиннику" in body


def test_plan_sync_button_hidden_when_toggle_off(auth_client):
    client, uid = auth_client
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    _seed_plan_with_workout(uid, date=fut, week=1, type="easy", dist_km=5.0, status="planned")
    _set_sync_enabled(uid, False)
    body = client.get("/plan").text
    assert "Синхронізувати зараз" not in body


def test_plan_sync_post_skips_when_toggle_off(auth_client):
    from app.routers import plan as plan_router

    client, uid = auth_client
    _set_sync_enabled(uid, False)
    with patch.object(plan_router, "user_runtime", _fake_runtime), \
         patch.object(plan_router.plan_sync, "sync_plan_to_garmin", AsyncMock()) as m:
        r = client.post("/plan/sync", follow_redirects=False)
    assert r.status_code == 303
    m.assert_not_called()


def test_plan_sync_post_success_shows_ok(auth_client):
    from app.routers import plan as plan_router

    client, uid = auth_client
    with patch.object(plan_router, "user_runtime", _fake_runtime), \
         patch.object(plan_router.plan_sync, "sync_plan_to_garmin",
                       AsyncMock(return_value={"pushed": 1, "removed": 0, "errors": []})) as m:
        r = client.post("/plan/sync", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/plan?synced=ok"
    m.assert_called_once()


def test_plan_sync_post_reports_errors(auth_client):
    from app.routers import plan as plan_router

    client, uid = auth_client
    errors = [{"workout_id": 9, "step": "push", "msg": "boom"}]
    with patch.object(plan_router, "user_runtime", _fake_runtime), \
         patch.object(plan_router.plan_sync, "sync_plan_to_garmin",
                       AsyncMock(return_value={"pushed": 0, "removed": 0, "errors": errors})):
        r = client.post("/plan/sync", follow_redirects=False)
    assert r.headers["location"] == "/plan?synced=err"


def test_plan_sync_post_rate_limited_on_double_tap(auth_client):
    from app.routers import plan as plan_router

    client, uid = auth_client
    with patch.object(plan_router, "user_runtime", _fake_runtime), \
         patch.object(plan_router.plan_sync, "sync_plan_to_garmin",
                       AsyncMock(return_value={"pushed": 0, "removed": 0, "errors": []})) as m:
        client.post("/plan/sync", follow_redirects=False)
        r2 = client.post("/plan/sync", follow_redirects=False)
    assert r2.headers["location"] == "/plan?synced=wait"
    assert m.call_count == 1


def test_plan_page_shows_last_sync_summary(auth_client):
    client, uid = auth_client
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    plan_id = _seed_plan_with_workout(uid, date=fut, week=1, type="easy", status="planned")

    async def seed_summary():
        async with async_session_maker() as s:
            await repository.set_plan_sync_summary(s, uid, plan_id, 2, 1, [])

    anyio.run(seed_summary)
    body = client.get("/plan").text
    assert "Останній синк" in body
    assert "+2 / -1" in body
