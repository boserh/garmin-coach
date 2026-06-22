"""Web layer smoke tests via FastAPI TestClient (Garmin login mocked)."""
from unittest.mock import patch

import anyio
import pytest
from fastapi.testclient import TestClient

from app.core import security
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


def test_status(client):
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["garmin_login"] == "ok"
    assert "cost_usd_total" in body


def test_history_requires_token(client, monkeypatch):
    monkeypatch.setattr(security.settings, "WEB_TOKEN", "secret")
    assert client.get("/history").status_code == 401
    assert client.get("/history", headers={"X-Token": "secret"}).status_code == 200


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
            today = dt.date.today()
            for i, hrv in enumerate([58, 61, 55, 63]):
                d = (today - dt.timedelta(days=i)).isoformat()
                await repository.upsert_daily(
                    s, DailySummary(date=d, hrv_avg=hrv, sleep_h=7.0 + i * 0.2,
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
