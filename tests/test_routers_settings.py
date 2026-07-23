"""Web smoke tests: /settings creds, timezone, Garmin connect + MFA flow."""

import anyio
import pytest

from app.db import users
from app.db.base import async_session_maker
from tests.web_helpers import _seed_user


@pytest.fixture
def crypto_key(monkeypatch):
    from cryptography.fernet import Fernet

    from app.core import crypto

    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet", None)


def test_settings_saves_encrypted_credentials(auth_client, crypto_key):
    from app.core import crypto

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


def test_settings_saves_valid_timezone(auth_client, crypto_key):

    r = auth_client.post(
        "/settings", data={"timezone": "America/New_York"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=1"

    async def check():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, "t@example.com")
            assert u.timezone == "America/New_York"

    anyio.run(check)


def test_settings_rejects_garbage_timezone(auth_client, crypto_key):

    r = auth_client.post(
        "/settings", data={"timezone": "Not/AZone"}, follow_redirects=False,
    )
    assert r.headers["location"] == "/settings?tz=fail"

    async def check():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, "t@example.com")
            assert u.timezone != "Not/AZone"   # rejected — left at whatever it was

    anyio.run(check)


def _mfa_client(client, crypto_key, email):
    """A logged-in client for a fresh dedicated user — the shared auth_client user
    accumulates Fernet-encrypted fields across tests, which breaks decryption once
    crypto_key mints a new random key per test (see test_change_own_password)."""
    _seed_user(email=email, password="pw", is_admin=False)
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def test_garmin_connect_requires_creds(client, crypto_key):
    c = _mfa_client(client, crypto_key, "mfa1@example.com")
    r = c.post("/settings/garmin-connect", follow_redirects=False)
    assert r.headers["location"] == "/settings?garmin=nocreds"


def test_garmin_connect_success_saves_token(client, crypto_key, monkeypatch):
    email = "mfa2@example.com"
    c = _mfa_client(client, crypto_key, email)
    c.post("/settings", data={
        "garmin_email": "g@example.com", "garmin_password": "garminpass",
    })

    class FakeProvider:
        new_token = "fresh-token"

        def login(self):
            pass

    from app.routers import settings as settings_router
    monkeypatch.setattr(
        settings_router.providers, "build_user_provider", lambda creds: FakeProvider()
    )

    r = c.post("/settings/garmin-connect", follow_redirects=False)
    assert r.headers["location"] == "/settings?garmin=ok"

    from app.core import crypto

    async def check():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, email)
            assert crypto.decrypt(u.garth_token_enc) == "fresh-token"

    anyio.run(check)


def test_garmin_connect_mfa_redirects_and_shows_form(client, crypto_key, monkeypatch):
    c = _mfa_client(client, crypto_key, "mfa3@example.com")
    c.post("/settings", data={
        "garmin_email": "g@example.com", "garmin_password": "garminpass",
    })

    from app.garmin.mfa import MFARequired

    class FakeProvider:
        new_token = None

        def login(self):
            raise MFARequired(1)

    from app.routers import settings as settings_router
    monkeypatch.setattr(
        settings_router.providers, "build_user_provider", lambda creds: FakeProvider()
    )

    r = c.post("/settings/garmin-connect", follow_redirects=False)
    assert r.headers["location"] == "/settings?garmin=mfa"
    assert "Код підтвердження" in c.get("/settings?garmin=mfa").text


def test_garmin_mfa_submit_success(client, crypto_key, monkeypatch):
    email = "mfa4@example.com"
    c = _mfa_client(client, crypto_key, email)

    from app.routers import settings as settings_router
    monkeypatch.setattr(settings_router.mfa, "submit_code", lambda uid, code: "mfa-token")

    r = c.post(
        "/settings/garmin-mfa", data={"code": "123456"}, follow_redirects=False
    )
    assert r.headers["location"] == "/settings?garmin=ok"

    from app.core import crypto

    async def check():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, email)
            assert crypto.decrypt(u.garth_token_enc) == "mfa-token"

    anyio.run(check)


def test_garmin_mfa_submit_expired(client, crypto_key, monkeypatch):
    c = _mfa_client(client, crypto_key, "mfa5@example.com")

    from app.garmin.mfa import MFANotPending
    from app.routers import settings as settings_router

    def raise_expired(uid, code):
        raise MFANotPending("gone")

    monkeypatch.setattr(settings_router.mfa, "submit_code", raise_expired)

    r = c.post(
        "/settings/garmin-mfa", data={"code": "000000"}, follow_redirects=False
    )
    assert r.headers["location"] == "/settings?garmin=expired"


