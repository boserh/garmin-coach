"""Test setup: point the app at a throwaway SQLite file before anything imports
the engine, and provide an isolated in-memory session fixture."""
import os

# Must run before any app.* import pulls in the engine from Settings.
# Hard-override (not setdefault): a DATABASE_URL exported in the shell or set in
# .env must NEVER leak the real garmin.db into the test run.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_garmin.db"
os.environ["WEB_TOKEN"] = ""
# SEC-01: disable the login/register rate limiter globally — the router tests log in
# many times in a row with the same email. Dedicated rate-limit tests build their own
# limiter instead of relying on this default.
os.environ["LOGIN_RATE_LIMIT"] = "0"
# Cost safety (see CLAUDE.md "Cost safety"): tests must NEVER reach the real Anthropic API.
# Hard-override the key to a dummy so even a mock that misses its target gets a 401 instead
# of spending real money — CODE-01's refactor silently un-mocked calls and burned tokens.
os.environ["ANTHROPIC_API_KEY"] = "test-dummy-key-not-real"

# Start from a clean schema each run — init_db() only create_all's, it won't ALTER a
# stale file left over from an older schema.
for _f in ("test_garmin.db", "test_garmin.db-wal", "test_garmin.db-journal"):
    try:
        os.remove(_f)
    except FileNotFoundError:
        pass

import anthropic  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.db.models  # noqa: E402,F401 — register models on Base.metadata
from app.db.base import Base  # noqa: E402


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
