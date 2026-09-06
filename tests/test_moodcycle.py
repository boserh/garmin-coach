"""NF-35 · the daytime mood/energy check-in and its pure pattern detector.

Mirrors NF-28's test shape: the risk here is confident nonsense on a thin diary (a fake
weekly pattern from one bad Tuesday, a fake "cycle" from a handful of coincidental points),
so most of these tests are about what must NOT be reported.
"""
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app import moodcycle
from app.db import lifestyle as lifestyle_db
from app.db.models import User


async def _user(session, mood_tracking_enabled=True) -> User:
    u = User(email="mood@example.com", password_hash="x",
             mood_tracking_enabled=mood_tracking_enabled)
    session.add(u)
    await session.commit()
    return u


def _dates(start: dt.date, n: int):
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(n)]


# ---------- storage ----------

@pytest.mark.asyncio
async def test_daytime_fields_are_independent_of_evening_tags(session):
    """A daytime tap must not clobber the evening upsert, or vice versa."""
    u = await _user(session)
    day = "2026-08-05"
    await lifestyle_db.upsert(session, u.id, day, ["alcohol"])
    await lifestyle_db.upsert_daytime(session, u.id, day, energy_level="tired")
    row = await lifestyle_db.get_day(session, u.id, day)
    assert row.tags == ["alcohol"]
    assert row.energy_level == "tired"


@pytest.mark.asyncio
async def test_daytime_taps_do_not_clobber_each_other(session):
    u = await _user(session)
    day = "2026-08-05"
    await lifestyle_db.upsert_daytime(session, u.id, day, energy_level="charged")
    await lifestyle_db.upsert_daytime(session, u.id, day, mood=4)
    row = await lifestyle_db.upsert_daytime(session, u.id, day, irritability=2)
    assert (row.energy_level, row.mood, row.irritability) == ("charged", 4, 2)


@pytest.mark.asyncio
async def test_read_range_carries_daytime_fields(session):
    u = await _user(session)
    day = dt.date.today().isoformat()
    await lifestyle_db.upsert_daytime(session, u.id, day, energy_level="ok", mood=3)
    rows = await lifestyle_db.read_range(session, u.id, days=30)
    assert rows[0]["energy_level"] == "ok" and rows[0]["mood"] == 3
    assert rows[0]["irritability"] is None


# ---------- pure detector ----------

def test_no_answers_yields_nothing():
    assert moodcycle.analyze([]) is None
    assert moodcycle.analyze([{"date": "2026-08-05", "tags": []}]) is None


def test_a_single_bad_day_does_not_become_a_weekly_pattern():
    """One data point per weekday cell is noise, not a pattern — it must not render."""
    start = dt.date(2026, 5, 4)  # a Monday
    logs = [{"date": d, "mood": 2 if i == 1 else 4, "tags": []}
            for i, d in enumerate(_dates(start, 7))]
    result = moodcycle.analyze(logs)
    assert result is None or result["weekly"] is None


def test_a_real_weekly_pattern_survives_with_enough_samples():
    """Every Tuesday scores worse, over several weeks — that's a real, reportable pattern."""
    start = dt.date(2026, 5, 4)  # a Monday
    n = 42
    logs = [{"date": d, "mood": 2 if i % 7 == 1 else 4, "tags": []}
            for i, d in enumerate(_dates(start, n))]
    result = moodcycle.analyze(logs)
    assert result is not None and result["weekly"] is not None
    tue = next(c for c in result["weekly"]["mood"] if c["label"] == "Вт")
    assert tue["avg"] == 2.0
    assert tue["n"] >= moodcycle.MIN_WEEKDAY_SAMPLES


def test_a_handful_of_points_never_reports_a_cycle():
    """Three weeks of data is not enough span to test any lag at all."""
    start = dt.date(2026, 5, 1)
    logs = [{"date": d, "mood": 3 + (i % 2), "tags": []} for i, d in enumerate(_dates(start, 15))]
    assert moodcycle._cycle_hint(moodcycle._numeric_series(logs, "mood")) is None


def test_a_strong_periodic_signal_is_detected_with_enough_span():
    """A clean ~28-day period, with enough answered days to pair across it, must surface —
    otherwise the feature never finds the thing the user actually asked for."""
    start = dt.date(2026, 1, 1)
    n = 70
    logs = [{"date": d, "mood": 2 if (i % 28) < 4 else 4, "tags": []}
            for i, d in enumerate(_dates(start, n))]
    result = moodcycle.analyze(logs)
    assert result is not None and result["cycle"] is not None
    assert moodcycle.CYCLE_MIN_LAG <= result["cycle"]["lag"] <= moodcycle.CYCLE_MAX_LAG


# ---------- bot surface ----------

def test_daytime_keyboard_callback_data_carries_the_date():
    from bot.handlers import daytime_keyboard

    kb = daytime_keyboard("2026-08-05")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    # Header buttons ("dt:noop") are pure captions, not picks — they carry no date.
    picks = [d for d in data if d != "dt:noop"]
    assert picks and all(d.startswith("dt:") and "2026-08-05" in d for d in picks)


def test_daytime_keyboard_groups_are_labelled():
    """The reported bug: two bare 1-5 rows back to back (mood, irritability) were
    indistinguishable, and a tap meant for one landed as the other. Every group now has a
    header and every button carries its own text, not a bare number."""
    from bot.handlers import daytime_keyboard

    labels = [b.text for row in daytime_keyboard("2026-08-05").inline_keyboard for b in row]
    assert any("Настрій" in t for t in labels)
    assert any("Роздратованість" in t for t in labels)
    assert not any(t.strip().isdigit() for t in labels)


class _FakeCBQ:
    def __init__(self, data, chat_id, text):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), text=text)
        self.edits = []

    async def answer(self, *a, **kw):
        pass

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


@pytest.fixture
def bot_session(session, monkeypatch):
    import bot.handlers as handlers

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(handlers, "async_session_maker", maker)
    return session


async def _tap(session, data, chat_id):
    import bot.handlers as handlers

    q = _FakeCBQ(data, chat_id, handlers.DAYTIME_PROMPT)
    await handlers.daytime_callback(SimpleNamespace(callback_query=q), None)
    return q.edits[-1]


async def _linked_user(session, chat_id, mood_tracking_enabled=True):
    u = User(email=f"mood{chat_id}@example.com", password_hash="x",
             telegram_chat_id=chat_id, is_active=True, is_approved=True,
             mood_tracking_enabled=mood_tracking_enabled)
    session.add(u)
    await session.commit()
    return u


async def test_energy_tap_stores_and_keeps_the_keyboard_open(bot_session):
    await _linked_user(bot_session, 910001)
    text, markup = await _tap(bot_session, "dt:e:2026-08-05:tired", 910001)
    assert "втомлений" in text
    assert markup is not None


async def test_done_offers_a_way_back_in(bot_session):
    """Closing must not be final — a wrong tap on a 5-point scale is common, and the
    reported complaint was exactly that: no way to change an answer after the fact."""
    user = await _linked_user(bot_session, 910002)
    await _tap(bot_session, "dt:m:2026-08-05:4", 910002)
    text, markup = await _tap(bot_session, "dt:done:2026-08-05", 910002)
    row = await lifestyle_db.get_day(bot_session, user.id, "2026-08-05")
    assert row.mood == 4
    assert markup is not None
    labels = [b.text for r in markup.inline_keyboard for b in r]
    assert any("Змінити" in t for t in labels)


async def test_edit_reopens_the_full_keyboard_and_the_change_sticks(bot_session):
    user = await _linked_user(bot_session, 910004)
    await _tap(bot_session, "dt:m:2026-08-05:4", 910004)
    await _tap(bot_session, "dt:done:2026-08-05", 910004)
    text, markup = await _tap(bot_session, "dt:edit:2026-08-05", 910004)
    assert markup is not None and len(markup.inline_keyboard) > 1
    await _tap(bot_session, "dt:m:2026-08-05:2", 910004)
    row = await lifestyle_db.get_day(bot_session, user.id, "2026-08-05")
    assert row.mood == 2


async def test_header_tap_is_a_noop(bot_session):
    """A row-header button is a caption, not a pick — tapping it must not write anything."""
    await _linked_user(bot_session, 910005)
    q = _FakeCBQ("dt:noop", 910005, "whatever")
    import bot.handlers as handlers
    await handlers.daytime_callback(SimpleNamespace(callback_query=q), None)
    assert q.edits == []


async def test_disabled_toggle_refuses_a_stale_tap(bot_session):
    """A user who turned tracking off after the prompt went out shouldn't have a stale
    button silently write data again."""
    await _linked_user(bot_session, 910003, mood_tracking_enabled=False)
    text, markup = await _tap(bot_session, "dt:e:2026-08-05:ok", 910003)
    assert "вимкнено" in text.lower()
    assert markup is None
