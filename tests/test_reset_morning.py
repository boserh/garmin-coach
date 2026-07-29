"""scripts/reset_morning.py — purge the Claude dedup cache so the next morning report
is really generated (and, optionally, let today's morning DM fire again).

The DB helpers are tested directly against the in-memory session; the argparse/asyncio
shell around them is trivial and left alone.
"""
import datetime as dt
import time

import pytest

from app.core.crypto import hash_password
from app.db.models import BotState, LlmCache, User
from scripts.reset_morning import (
    MORNING_STATE_KEY,
    UserNotFound,
    clear_morning_guard,
    purge_cache,
)


async def _seed_cache(session, *, ages_days=(0, 0)):
    for i, age in enumerate(ages_days):
        session.add(LlmCache(
            key=f"{i:064d}", value="старий звіт", expires_at=time.time() + 999,
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age),
        ))
    await session.commit()


async def _seed_user(session, email="me@example.com", guard="2026-07-29"):
    user = User(email=email, password_hash=hash_password("x"))
    session.add(user)
    await session.flush()
    if guard is not None:
        session.add(BotState(user_id=user.id, key=MORNING_STATE_KEY, value=guard))
    await session.commit()
    return user


async def test_purge_cache_removes_recent_entries(session):
    await _seed_cache(session, ages_days=(0, 0))
    assert await purge_cache(session, days=1, dry_run=False) == 2
    assert await purge_cache(session, days=1, dry_run=False) == 0


async def test_purge_cache_days_window_keeps_older_entries(session):
    await _seed_cache(session, ages_days=(0, 5))       # today + five days old
    assert await purge_cache(session, days=1, dry_run=False) == 1
    # the old one survived a 1-day purge and only goes with the full sweep
    assert await purge_cache(session, days=None, dry_run=False) == 1


async def test_purge_cache_dry_run_deletes_nothing(session):
    await _seed_cache(session, ages_days=(0, 0))
    assert await purge_cache(session, days=1, dry_run=True) == 2
    assert await purge_cache(session, days=1, dry_run=False) == 2   # still there


async def test_clear_morning_guard_returns_and_clears_the_value(session):
    user = await _seed_user(session)
    assert await clear_morning_guard(session, user.email, dry_run=False) == "2026-07-29"
    assert await session.get(BotState, (user.id, MORNING_STATE_KEY)) is None
    # idempotent: nothing left to clear
    assert await clear_morning_guard(session, user.email, dry_run=False) is None


async def test_clear_morning_guard_dry_run_keeps_the_guard(session):
    user = await _seed_user(session)
    assert await clear_morning_guard(session, user.email, dry_run=True) == "2026-07-29"
    row = await session.get(BotState, (user.id, MORNING_STATE_KEY))
    assert row is not None and row.value == "2026-07-29"


async def test_clear_morning_guard_unknown_user_raises(session):
    with pytest.raises(UserNotFound):
        await clear_morning_guard(session, "nobody@example.com", dry_run=False)
