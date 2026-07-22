"""ST-11: garth token expiry warning in the morning tick."""
import base64
import datetime as dt
import json

import pytest
from cryptography.fernet import Fernet

from app.core import crypto
from app.db.models import User


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.fake-signature"


def _token_blob(issued: dt.date) -> str:
    iat = int(dt.datetime(issued.year, issued.month, issued.day,
                          tzinfo=dt.timezone.utc).timestamp())
    oauth1 = {"oauth_token": "t", "oauth_token_secret": "s", "domain": "garmin.com"}
    oauth2 = {
        "scope": "CONNECT_READ", "jti": "x", "token_type": "Bearer",
        "access_token": _jwt({"iat": iat, "exp": iat + 3600}),
        "refresh_token": "r", "expires_in": 3600, "expires_at": iat + 3600,
        "refresh_token_expires_in": 86400, "refresh_token_expires_at": iat + 86400,
    }
    return base64.b64encode(json.dumps([oauth1, oauth2]).encode()).decode()


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", Fernet.generate_key().decode())


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class _FakeCtx:
    def __init__(self):
        self.bot = _FakeBot()


async def _make_user(session, issued: dt.date) -> User:
    token = crypto.encrypt(_token_blob(issued))
    user = User(email="tok@x.com", password_hash="x", telegram_chat_id=999,
                garth_token_enc=token)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_token_expiry_silent_when_far_from_death(session):
    from bot import jobs

    today = dt.date.today()
    user = await _make_user(session, today - dt.timedelta(days=10))  # ~355 days left
    ctx = _FakeCtx()
    await jobs._token_expiry_check_for_user(ctx, session, user)
    assert ctx.bot.sent == []


async def test_token_expiry_warns_at_30_day_threshold(session):
    from bot import jobs

    today = dt.date.today()
    issued = today - dt.timedelta(days=365 - 20)  # ~20 days left
    user = await _make_user(session, issued)
    ctx = _FakeCtx()
    await jobs._token_expiry_check_for_user(ctx, session, user)
    assert len(ctx.bot.sent) == 1
    assert "Garmin" in ctx.bot.sent[0][1]


async def test_token_expiry_no_repeat_same_threshold(session):
    from bot import jobs

    today = dt.date.today()
    issued = today - dt.timedelta(days=365 - 20)
    user = await _make_user(session, issued)
    ctx = _FakeCtx()
    await jobs._token_expiry_check_for_user(ctx, session, user)
    await jobs._token_expiry_check_for_user(ctx, session, user)
    assert len(ctx.bot.sent) == 1


async def test_token_expiry_warns_again_at_7_day_threshold(session):
    from bot import jobs

    today = dt.date.today()
    issued = today - dt.timedelta(days=365 - 20)
    user = await _make_user(session, issued)
    ctx = _FakeCtx()
    await jobs._token_expiry_check_for_user(ctx, session, user)  # 30d threshold fires

    # Time passes: now within the 7-day threshold too.
    issued7 = today - dt.timedelta(days=365 - 5)
    token7 = crypto.encrypt(_token_blob(issued7))
    user.garth_token_enc = token7
    await session.commit()
    await jobs._token_expiry_check_for_user(ctx, session, user)
    assert len(ctx.bot.sent) == 2


async def test_token_expiry_guard_resets_after_relogin(session):
    from bot import jobs

    today = dt.date.today()
    issued = today - dt.timedelta(days=365 - 20)
    user = await _make_user(session, issued)
    ctx = _FakeCtx()
    await jobs._token_expiry_check_for_user(ctx, session, user)
    assert len(ctx.bot.sent) == 1

    # A fresh re-login mints a new token with a new issue date near the same threshold —
    # the guard's stored issue date no longer matches, so it warns again.
    new_issued = today - dt.timedelta(days=365 - 25)
    user.garth_token_enc = crypto.encrypt(_token_blob(new_issued))
    await session.commit()
    await jobs._token_expiry_check_for_user(ctx, session, user)
    assert len(ctx.bot.sent) == 2


async def test_token_expiry_no_token_is_silent(session):
    from bot import jobs

    user = User(email="notok@x.com", password_hash="x", telegram_chat_id=1000)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    ctx = _FakeCtx()
    await jobs._token_expiry_check_for_user(ctx, session, user)
    assert ctx.bot.sent == []


async def test_token_expiry_undecodable_token_is_silent(session):
    from bot import jobs

    user = User(email="bad@x.com", password_hash="x", telegram_chat_id=1001,
                garth_token_enc=crypto.encrypt("not a garth token"))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    ctx = _FakeCtx()
    await jobs._token_expiry_check_for_user(ctx, session, user)
    assert ctx.bot.sent == []


async def test_token_expiry_no_chat_id_is_silent(session):
    from bot import jobs

    today = dt.date.today()
    token = crypto.encrypt(_token_blob(today - dt.timedelta(days=365 - 5)))
    user = User(email="nochat@x.com", password_hash="x", garth_token_enc=token)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    ctx = _FakeCtx()
    await jobs._token_expiry_check_for_user(ctx, session, user)
    assert ctx.bot.sent == []
