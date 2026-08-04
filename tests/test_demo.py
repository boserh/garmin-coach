"""Demo account (see app.demo / app.core.demo): a one-click walkthrough with seeded
fake data that must never reach Garmin or Anthropic. conftest's autouse
``_no_real_anthropic`` fixture already blocks the real SDK client — these tests check
the guards fire BEFORE that point is even reached (i.e. no Garmin call is attempted
either, which _no_real_anthropic can't see)."""
import anyio

from app.db import users as users_db
from app.db.base import async_session_maker
from app.db.models import User


def test_demo_login_creates_and_reuses_singleton(client):
    r = client.post("/demo-login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert client.get("/dashboard").status_code == 200

    async def count_demo_users():
        async with async_session_maker() as s:
            from sqlalchemy import func, select
            n = (await s.execute(
                select(func.count()).select_from(User).where(User.is_demo.is_(True))
            )).scalar_one()
            return n

    assert anyio.run(count_demo_users) == 1

    # a second click reuses the same account, doesn't create another one
    r2 = client.post("/demo-login", follow_redirects=False)
    assert r2.status_code == 303
    assert anyio.run(count_demo_users) == 1


def test_demo_report_and_deep_never_call_out(client):
    client.post("/demo-login")
    report = client.get("/report.json")
    assert report.status_code == 200
    assert "демо" in report.json()["report"].lower()

    deep = client.get("/deep")
    assert deep.status_code == 200
    assert "демо" in deep.json()["report"].lower()


def test_demo_chat_send_is_short_circuited(client):
    client.post("/demo-login")
    r = client.post("/chat", data={"message": "як мій сон?"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/chat?err=")


def test_demo_plan_generation_blocked(client):
    client.post("/demo-login")
    r = client.post(
        "/plan",
        data={"goal": "first_5k", "run_days": ["tue", "thu"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/plan?error=demo"


def test_demo_garmin_connect_blocked(client):
    client.post("/demo-login")
    r = client.post("/settings/garmin-connect", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?garmin=demo"


def test_eligible_users_excludes_demo():
    async def run():
        async with async_session_maker() as s:
            from app.demo import ensure_demo_user

            demo = await ensure_demo_user(s)
            regular = await users_db.create_user(
                s, email="regular@example.com", password_hash="x",
                is_admin=False, is_approved=True,
            )
            pool = await users_db.eligible_users(s)
            ids = {u.id for u in pool}
            assert demo.id not in ids
            assert regular.id in ids

    anyio.run(run)


def test_user_runtime_refuses_demo_account():
    from app.garmin.runtime import DemoModeUnavailable, user_runtime

    async def run():
        async with async_session_maker() as s:
            from app.demo import ensure_demo_user

            demo = await ensure_demo_user(s)
            try:
                async with user_runtime(s, demo):
                    pass
                raise AssertionError("user_runtime should have refused the demo account")
            except DemoModeUnavailable:
                pass

    anyio.run(run)


def test_get_client_refuses_when_demo_context_set():
    from app.analysis.client import IS_DEMO, AnalystError, _get_client

    token = IS_DEMO.set(True)
    try:
        try:
            _get_client("some-key")
            raise AssertionError("_get_client should have refused under IS_DEMO")
        except AnalystError as e:
            assert "демо" in str(e).lower()
    finally:
        IS_DEMO.reset(token)
