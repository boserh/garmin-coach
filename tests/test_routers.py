"""Web layer smoke tests via FastAPI TestClient (Garmin login mocked)."""
from unittest.mock import patch

import anyio
import pytest
from fastapi.testclient import TestClient

from app.core.crypto import hash_password
from app.db import users
from app.garmin import service
from app.main import create_app


@pytest.fixture
def client():
    with patch.object(service, "login", return_value=None):
        with TestClient(create_app()) as c:
            yield c


def _seed_user(email="t@example.com", password="pw", is_admin=True):
    from app.db.base import async_session_maker

    async def seed():
        async with async_session_maker() as s:
            if not await users.get_by_email(s, email):
                await users.create_user(
                    s, email=email, password_hash=hash_password(password), is_admin=is_admin
                )

    anyio.run(seed)


@pytest.fixture
def auth_client(client):
    """A TestClient with a logged-in session cookie."""
    _seed_user()
    r = client.post("/login", data={"email": "t@example.com", "password": "pw"})
    assert r.status_code == 200  # followed the redirect to /ui
    return client


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_status_requires_login(client):
    assert client.get("/status", follow_redirects=False).status_code == 303


def test_status(auth_client):
    r = auth_client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["garmin_login"] == "ok"
    assert "cost_usd_total" in body


def test_history_requires_login(client):
    assert client.get("/history", follow_redirects=False).status_code == 303


def test_history_when_logged_in(auth_client):
    r = auth_client.get("/history")
    assert r.status_code == 200
    assert "trend" in r.json()


def test_ui_requires_login(client):
    # unauthenticated → redirect to /login
    r = client.get("/ui", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_flow(client):
    _seed_user()
    assert client.get("/login").status_code == 200
    bad = client.post(
        "/login", data={"email": "t@example.com", "password": "nope"},
        follow_redirects=False,
    )
    assert bad.status_code == 401
    ok = client.post(
        "/login", data={"email": "t@example.com", "password": "pw"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "/ui"


def test_ui_browse(auth_client):
    assert auth_client.get("/ui").status_code == 200
    r = auth_client.get("/ui/daily_metrics")
    assert r.status_code == 200
    assert "daily_metrics" in r.text
    assert auth_client.get("/ui/nope").status_code == 404


def test_logout_clears_session(auth_client):
    assert auth_client.get("/ui", follow_redirects=False).status_code == 200
    auth_client.get("/logout")
    assert auth_client.get("/ui", follow_redirects=False).status_code == 303


def _user_id(email):
    from app.db.base import async_session_maker

    async def get():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, email)
            return u.id if u else None

    return anyio.run(get)


def test_register_creates_pending_user_and_blocks_login(client):
    r = client.post(
        "/register", data={"email": "newbie@example.com", "password": "secret1"},
        follow_redirects=False,
    )
    assert r.status_code == 200  # login page with an "awaiting approval" notice

    from app.db.base import async_session_maker

    async def check():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, "newbie@example.com")
            assert u is not None
            assert u.is_approved is False
            assert u.is_admin is False

    anyio.run(check)

    # unapproved → cannot log in yet
    login = client.post(
        "/login", data={"email": "newbie@example.com", "password": "secret1"},
        follow_redirects=False,
    )
    assert login.status_code == 403


def test_register_rejects_duplicate_email(auth_client):
    # t@example.com already exists (the admin)
    r = auth_client.post(
        "/register", data={"email": "t@example.com", "password": "secret1"},
        follow_redirects=False,
    )
    assert r.status_code == 409


def test_admin_approves_then_user_can_login(auth_client):
    auth_client.post("/register", data={"email": "pend@example.com", "password": "secret1"})
    uid = _user_id("pend@example.com")

    r = auth_client.post(f"/admin/users/{uid}/approve", follow_redirects=False)
    assert r.status_code == 303

    # logging in here replaces the admin session cookie with the approved user's
    login = auth_client.post(
        "/login", data={"email": "pend@example.com", "password": "secret1"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/settings"  # non-admin lands on settings


def test_admin_deletes_user(auth_client):
    auth_client.post("/register", data={"email": "del@example.com", "password": "secret1"})
    uid = _user_id("del@example.com")
    r = auth_client.post(f"/admin/users/{uid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert _user_id("del@example.com") is None


def _seed_two_users_with_data():
    """alice + bob, each with one daily metric and one report. Returns their ids."""
    from app.db.base import async_session_maker
    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    _seed_user(email="alice@example.com", password="pw", is_admin=False)
    _seed_user(email="bob@example.com", password="pw", is_admin=False)
    aid, bid = _user_id("alice@example.com"), _user_id("bob@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.upsert_daily(
                s, aid, DailySummary(date="2026-06-20", hrv_avg=55, has_data=True))
            await repository.upsert_daily(
                s, bid, DailySummary(date="2026-06-20", hrv_avg=70, has_data=True))
            await repository.log_report(s, user_id=aid, kind="report", model="m",
                                        ok=True, report_text="alice report")
            await repository.log_report(s, user_id=bid, kind="report", model="m",
                                        ok=True, report_text="bob report")
            await s.commit()

    anyio.run(seed)
    return aid, bid


def _report_id(user_id):
    from sqlalchemy import select

    from app.db.base import async_session_maker
    from app.db.models import ReportLog

    async def get():
        async with async_session_maker() as s:
            return (await s.execute(
                select(ReportLog.id).where(ReportLog.user_id == user_id)
                .order_by(ReportLog.id.desc()).limit(1)
            )).scalar_one()

    return anyio.run(get)


def test_info_requires_login(client):
    assert client.get("/info", follow_redirects=False).status_code == 303


def test_admin_clears_bot_state(auth_client):
    from app.db.base import async_session_maker
    from app.garmin import repository

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.set_state(s, uid, "morning_sent_date", "2026-06-24")

    anyio.run(seed)
    assert "morning_sent_date" in auth_client.get("/ui/bot_state").text

    r = auth_client.post(
        "/ui/bot_state/delete",
        data={"user_id": str(uid), "key": "morning_sent_date"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    async def check():
        async with async_session_maker() as s:
            return await repository.get_state(s, uid, "morning_sent_date")

    assert anyio.run(check) is None


def test_info_when_logged_in(auth_client):
    r = auth_client.get("/info")
    assert r.status_code == 200
    assert "Як це працює" in r.text


def test_me_requires_login(client):
    assert client.get("/me", follow_redirects=False).status_code == 303


def test_me_shows_only_own_data(client):
    _seed_two_users_with_data()
    client.post("/login", data={"email": "alice@example.com", "password": "pw"})

    assert client.get("/me").status_code == 200
    rl = client.get("/me/report_logs")
    assert rl.status_code == 200
    assert "alice report" in rl.text
    assert "bob report" not in rl.text          # other user's data not visible

    # user-facing browser exposes only the three data tables
    assert client.get("/me/users").status_code == 404
    assert client.get("/me/bot_state").status_code == 404


def test_me_row_isolation(client):
    aid, bid = _seed_two_users_with_data()
    client.post("/login", data={"email": "alice@example.com", "password": "pw"})

    assert client.get(f"/me/report_logs/{_report_id(aid)}").status_code == 200
    # alice cannot open bob's row
    assert client.get(f"/me/report_logs/{_report_id(bid)}").status_code == 404


def test_admin_deactivate_and_reactivate(auth_client):
    _seed_user(email="da@example.com", password="pw", is_admin=False)
    uid = _user_id("da@example.com")

    # deactivate → that user can no longer log in
    assert auth_client.post(
        f"/admin/users/{uid}/active", data={"active": "0"}, follow_redirects=False
    ).status_code == 303
    blocked = auth_client.post(
        "/login", data={"email": "da@example.com", "password": "pw"},
        follow_redirects=False,
    )
    assert blocked.status_code == 403  # "Акаунт деактивовано" — admin session intact

    # reactivate → login works again
    assert auth_client.post(
        f"/admin/users/{uid}/active", data={"active": "1"}, follow_redirects=False
    ).status_code == 303
    assert auth_client.post(
        "/login", data={"email": "da@example.com", "password": "pw"},
        follow_redirects=False,
    ).status_code == 303


def test_admin_cannot_deactivate_self(auth_client):
    uid = _user_id("t@example.com")
    auth_client.post(f"/admin/users/{uid}/active", data={"active": "0"},
                     follow_redirects=False)

    from app.db.base import async_session_maker

    async def check():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, "t@example.com")
            assert u.is_active is True  # self-deactivation ignored

    anyio.run(check)


def test_change_password_requires_login(client):
    r = client.post(
        "/settings/password",
        data={"current_password": "x", "new_password": "yyyyyy"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_change_own_password(client):
    _seed_user(email="pw1@example.com", password="origpass", is_admin=False)
    client.post("/login", data={"email": "pw1@example.com", "password": "origpass"})

    wrong = client.post(
        "/settings/password",
        data={"current_password": "nope", "new_password": "newpass1"},
        follow_redirects=False,
    )
    assert wrong.headers["location"] == "/settings?pw=wrong"

    short = client.post(
        "/settings/password",
        data={"current_password": "origpass", "new_password": "abc"},
        follow_redirects=False,
    )
    assert short.headers["location"] == "/settings?pw=short"

    ok = client.post(
        "/settings/password",
        data={"current_password": "origpass", "new_password": "newpass1"},
        follow_redirects=False,
    )
    assert ok.headers["location"] == "/settings?pw=ok"

    # old password stops working, new one logs in
    client.get("/logout")
    assert client.post(
        "/login", data={"email": "pw1@example.com", "password": "origpass"},
        follow_redirects=False,
    ).status_code == 401
    assert client.post(
        "/login", data={"email": "pw1@example.com", "password": "newpass1"},
        follow_redirects=False,
    ).status_code == 303


@pytest.fixture
def crypto_key(monkeypatch):
    from cryptography.fernet import Fernet

    from app.core import crypto

    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet", None)


def test_settings_saves_encrypted_credentials(auth_client, crypto_key):
    from app.core import crypto
    from app.db.base import async_session_maker

    r = auth_client.post(
        "/settings",
        data={
            "garmin_email": "g@example.com",
            "garmin_password": "garminpass",
            "anthropic_key": "sk-ant-xyz",
            "telegram_chat_id": "123456",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=1"

    async def check():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, "t@example.com")
            assert u.garmin_password_enc and u.garmin_password_enc != "garminpass"
            assert crypto.decrypt(u.garmin_password_enc) == "garminpass"
            assert crypto.decrypt(u.anthropic_key_enc) == "sk-ant-xyz"
            assert crypto.decrypt(u.garmin_email_enc) == "g@example.com"
            assert u.telegram_chat_id == 123456

    anyio.run(check)


def test_admin_users_create(auth_client):
    r = auth_client.post(
        "/admin/users",
        data={"email": "new@example.com", "password": "pw2"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    from app.db.base import async_session_maker

    async def check():
        async with async_session_maker() as s:
            assert await users.get_by_email(s, "new@example.com") is not None

    anyio.run(check)


def test_admin_users_forbidden_for_non_admin(client):
    _seed_user(email="plain@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "plain@example.com", "password": "pw"})
    assert client.get("/admin/users").status_code == 403


def test_ui_daily_metrics_has_trend_chart(auth_client):
    import datetime as dt

    from app.db.base import async_session_maker
    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    async def seed():
        async with async_session_maker() as s:
            user = await users.get_by_email(s, "t@example.com")
            today = dt.date.today()
            for i, hrv in enumerate([58, 61, 55, 63]):
                d = (today - dt.timedelta(days=i)).isoformat()
                await repository.upsert_daily(
                    s, user.id, DailySummary(date=d, hrv_avg=hrv, sleep_h=7.0 + i * 0.2,
                                             sleep_score=80, has_data=True)
                )
            await s.commit()

    anyio.run(seed)
    body = auth_client.get("/ui/daily_metrics").text
    assert 'class="charts"' in body
    assert "HRV avg" in body
    assert "<polyline" in body
    # other tables get no chart
    assert 'class="charts"' not in auth_client.get("/ui/report_logs").text
