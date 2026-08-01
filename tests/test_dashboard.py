"""EP-04: the web dashboard — pure DB-read route, login-gated, mobile page."""
import datetime as dt
from unittest.mock import patch

import anyio
import pytest
from fastapi.testclient import TestClient

from app.core.crypto import hash_password
from app.db import users
from app.db.base import async_session_maker
from app.garmin import repository
from app.garmin.schemas import DailySummary
from app.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


async def _seed_user_async(email, password):
    async with async_session_maker() as session:
        if not await users.get_by_email(session, email):
            await users.create_user(
                session, email=email, password_hash=hash_password(password), is_admin=False,
            )


def _seed_user_sync(email, password):
    anyio.run(_seed_user_async, email, password)


def _login(client, email="dash@example.com", password="pw"):
    r = client.post("/login", data={"email": email, "password": password},
                     follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    return r


def test_dashboard_requires_login(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_non_admin_login_redirects_to_dashboard(client):
    _seed_user_sync("dash@example.com", "pw")
    _login(client)


def test_dashboard_empty_state(client):
    _seed_user_sync("dash@example.com", "pw")
    _login(client)
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Ще немає історії" in r.text
    assert "Немає активної програми" in r.text
    assert "Активностей поки немає" in r.text
    assert "AI цього місяця" in r.text


async def test_dashboard_with_data(client):
    await _seed_user_async("dash@example.com", "pw")
    _login(client)

    async with async_session_maker() as session:
        user = await users.get_by_email(session, "dash@example.com")
        today = dt.date.today()
        for i in range(5):
            d = (today - dt.timedelta(days=i)).isoformat()
            await repository.upsert_daily(session, user.id, DailySummary(
                date=d, hrv_avg=50 + i, sleep_h=7.0, sleep_score=70,
                stress_avg=30, bb_charged=60, has_data=True,
            ))
        await repository.upsert_activity(session, user.id, 1, {
            "date": today.isoformat(), "type": "running",
            "dist_km": 8.0, "dur_min": 45.0, "avg_hr": 150,
        })
        await repository.log_report(
            session, user_id=user.id, kind="report", model="m",
            input_tokens=100, output_tokens=50, cost_usd=0.01,
        )
        await session.commit()

    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "running" in r.text
    assert "8.0" in r.text
    assert "0.01" in r.text   # this month's AI cost
    assert "Ще немає історії" not in r.text

    # EP-17: the three rings always render, even when a metric behind one is missing —
    # only 5 days of history here, so the load ring (needs Garmin's acwr_pct, never
    # seeded) and the recovery ring (needs >=14 baseline samples) show the empty "—"
    # state instead of a fabricated number.
    assert "Навантаження" in r.text and "Відновлення" in r.text and "Сон" in r.text
    assert "HRV" in r.text and "Body Battery" in r.text and "Стрес" in r.text


async def test_dashboard_rings_with_full_data(client):
    """EP-17: enough history (>=14 days) for the recovery ring's NF-01 band, plus an
    ACWR + resting-HR day, so all three rings and every stat tile render real values."""
    await _seed_user_async("dash-rings@example.com", "pw")
    _login(client, email="dash-rings@example.com")

    async with async_session_maker() as session:
        user = await users.get_by_email(session, "dash-rings@example.com")
        today = dt.date.today()
        for i in range(20):
            d = (today - dt.timedelta(days=i)).isoformat()
            summary = DailySummary(
                date=d, hrv_avg=45 + i % 10, sleep_h=7.0, sleep_score=82,
                stress_avg=28, stress_max=55, bb_charged=64, has_data=True,
            )
            if i == 0:
                summary.extra = {"resting_hr": 48, "acwr_pct": 92}
            await repository.upsert_daily(session, user.id, summary)
        await session.commit()

    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "82" in r.text          # sleep score
    assert "92" in r.text          # ACWR%
    assert "48" in r.text          # resting HR tile
    assert "64" in r.text          # body battery tile
    assert "28" in r.text          # stress avg
    assert "макс 55" in r.text     # stress max, no fabricated "min"


async def test_dashboard_shows_upcoming_plan(client):
    await _seed_user_async("dash3@example.com", "pw")
    _login(client, email="dash3@example.com")

    from app.analysis import plans
    from app.analysis.client import CallStats
    from app.garmin.schemas import GeneratedPlan, PlanWorkout

    async with async_session_maker() as session:
        user = await users.get_by_email(session, "dash3@example.com")
        today = dt.date.today()
        gen = GeneratedPlan(summary="план", workouts=[
            PlanWorkout(date=(today + dt.timedelta(days=1)).isoformat(), week=1,
                        type="easy", dist_km=5.0, description="легко"),
            PlanWorkout(date=(today + dt.timedelta(days=30)).isoformat(), week=5,
                        type="long", dist_km=20.0, description="довгий"),
        ])
        with patch.object(plans, "generate_plan_with_stats",
                          return_value=(gen, CallStats(kind="plan", model="m"))):
            await plans.run_plan_generation(
                session, user_id=user.id, goal="first_5k", goal_label="x",
                target_date=None, start_date=today.isoformat(), days_per_week=3,
                intensity="easy", intake={}, api_key=None,
            )

    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "5.0" in r.text and "легко" in r.text
    assert "20.0" not in r.text   # 30 days out — outside the 7-day window
