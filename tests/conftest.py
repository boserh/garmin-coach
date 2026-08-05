"""Test setup: point the app at a throwaway SQLite file before anything imports
the engine, and provide an isolated in-memory session fixture."""
import functools
import os

# Must run before any app.* import pulls in the engine from Settings.
# Hard-override (not setdefault): a DATABASE_URL exported in the shell or set in
# .env must NEVER leak the real garmin.db into the test run.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_garmin.db"
# SEC-01: disable the login/register rate limiter globally — the router tests log in
# many times in a row with the same email. Dedicated rate-limit tests build their own
# limiter instead of relying on this default.
os.environ["LOGIN_RATE_LIMIT"] = "0"
# PERF-05's Garmin pacer is a real time.sleep() spacer, process-wide — against a
# FakeProvider it's pure wall-clock waste (real Garmin is never touched in tests).
# 0 disables it (app.core.config's own comment on GARMIN_RPS).
os.environ["GARMIN_RPS"] = "0"
# TestClient talks to http://testserver, and httpx (correctly) refuses to store a
# Secure cookie sent over plain HTTP — with the prod default every logged-in fixture
# would silently lose its session. Dedicated tests assert the flag is set in prod.
os.environ["SESSION_HTTPS_ONLY"] = "false"
# Cost safety (see CLAUDE.md "Cost safety"): tests must NEVER reach the real Anthropic API.
# Hard-override the key to a dummy so even a mock that misses its target gets a 401 instead
# of spending real money — CODE-01's refactor silently un-mocked calls and burned tokens.
os.environ["ANTHROPIC_API_KEY"] = "test-dummy-key-not-real"
# Same reasoning: a real TELEGRAM_ADMIN_BOT_TOKEN in .env must never make tests fire
# real Telegram alerts (app.core.alerts.TelegramAlertHandler forwards WARNING+ logs).
os.environ["TELEGRAM_ADMIN_BOT_TOKEN"] = ""

# Start from a clean schema each run — init_db() only create_all's, it won't ALTER a
# stale file left over from an older schema.
for _f in ("test_garmin.db", "test_garmin.db-wal", "test_garmin.db-journal"):
    try:
        os.remove(_f)
    except FileNotFoundError:
        pass

import anthropic  # noqa: E402
import bcrypt  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.db.models  # noqa: E402,F401 — register models on Base.metadata
from app.db.base import Base  # noqa: E402

# bcrypt's default cost (12 rounds) is deliberately slow (~100-300ms/hash) — a real
# security property in prod, pure wall-clock tax in tests, most of which seed a user
# (hash_password) at least once per test via _seed_user. Lowering the work factor still
# exercises the real bcrypt hash/verify round-trip, just fast; a test that specifically
# cares about cost (none currently do) can override rounds explicitly.
bcrypt.gensalt = functools.partial(bcrypt.gensalt, rounds=4)


class _BlockedAnthropic:
    """Belt-and-suspenders over the dummy key: any test that actually reaches the Anthropic
    client explodes here instead of hitting the live API — the net for a mock that misses
    its patch target. Tests that need a fake client override this with their own patch."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        raise AssertionError(
            f"Real Anthropic client used in a test (accessed {name!r}) — a Claude mock is "
            "missing its target. Patch the run_*/*_with_stats function or the client; the "
            "suite must never hit the live API."
        )


@pytest.fixture(autouse=True)
def _no_real_anthropic(monkeypatch):
    """Block the real Anthropic SDK in every test (cost safety)."""
    monkeypatch.setattr(anthropic, "Anthropic", _BlockedAnthropic)
    # Drop any cached client so the block also covers one built by an earlier test.
    try:
        from app.analysis import client as _client_mod

        _client_mod._clients.clear()
    except Exception:
        pass


class _BlockedGConnClient:
    """Mirrors _BlockedAnthropic for the native Garmin client. Without this, a POST
    /settings that flips garmin_sync_enabled (default True — see User model) runs
    plan_sync.unpush_all/sync_plan_to_garmin against a REAL garminconnect.Client before
    a test gets a chance to mock providers._gconn_client_cls itself — a real, slow
    (10-20s anti-WAF sleep + retries), flaky call out to actual Garmin/Cloudflare."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        raise AssertionError(
            f"Real garminconnect.Client used in a test (accessed {name!r}) — a Garmin "
            "mock is missing its target. Patch providers._gconn_client_cls (the "
            "designated import seam); the suite must never hit the live Garmin API."
        )


@pytest.fixture(autouse=True)
def _no_real_garmin(monkeypatch):
    """Block the real native Garmin client in every test (see _BlockedGConnClient).
    Tests that need specific provider behavior override providers._gconn_client_cls
    themselves, which takes precedence over this default."""
    from app.garmin import providers

    monkeypatch.setattr(providers, "_gconn_client_cls", lambda: _BlockedGConnClient)


@pytest.fixture
def client():
    """A FastAPI TestClient with Garmin login mocked — shared by the test_routers_* split
    (B3). A test module may still define its own ``client`` to override this."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.garmin import service
    from app.main import create_app

    with patch.object(service, "login", return_value=None):
        with TestClient(create_app()) as c:
            yield c


@pytest.fixture
def auth_client(client):
    """A TestClient with a logged-in admin session cookie (B3)."""
    from tests.web_helpers import _seed_user

    _seed_user()
    r = client.post("/login", data={"email": "t@example.com", "password": "pw"})
    assert r.status_code == 200  # followed the redirect to /ui
    return client


@pytest_asyncio.fixture
async def session():
    """A fresh in-memory SQLite session per test (shared connection via StaticPool)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()
