"""Health-checkup follow-ups: the pure `app.checkup_reminders` due/text helpers, the
daily `_checkup_reminder_for_user` job hook (bot/jobs.py, per-checkup once-only guard),
and the `/checkups` command."""
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import checkup_reminders
from app.db.models import HealthCheckup, User
from app.garmin import repository

U1 = 1


def _row(id=1, date="2026-07-01", title="Кров", next_due_date=None):
    return SimpleNamespace(id=id, date=date, title=title, next_due_date=next_due_date)


# --- pure due/text helpers -------------------------------------------------------

def test_due_includes_overdue_and_near_dates():
    today = dt.date(2026, 7, 31)
    rows = [
        _row(1, next_due_date="2026-07-20"),   # overdue
        _row(2, next_due_date="2026-08-05"),   # within 7-day lead
        _row(3, next_due_date="2026-09-01"),   # far out — not due
        _row(4, next_due_date=None),           # no reminder set
    ]
    due_ids = {r.id for r in checkup_reminders.due(rows, today)}
    assert due_ids == {1, 2}


def test_due_skips_malformed_date():
    today = dt.date(2026, 7, 31)
    rows = [_row(1, next_due_date="not-a-date")]
    assert checkup_reminders.due(rows, today) == []


def test_reminder_text_overdue_vs_upcoming():
    today = dt.date(2026, 7, 31)
    overdue = checkup_reminders.reminder_text(_row(next_due_date="2026-07-20"), today)
    assert "прострочено" in overdue
    soon = checkup_reminders.reminder_text(_row(next_due_date="2026-08-05"), today)
    assert "через" in soon
    exact = checkup_reminders.reminder_text(_row(next_due_date="2026-07-31"), today)
    assert "сьогодні" in exact


# --- bot/jobs.py daily reminder + once-only guard ---------------------------------

async def _mk_user(session, chat_id=555):
    u = User(email=f"{chat_id}@e.com", password_hash="h", is_approved=True,
             is_active=True, telegram_chat_id=chat_id, timezone="Europe/Warsaw")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


async def _mk_checkup(session, user_id, **kw):
    row = HealthCheckup(user_id=user_id, date=kw.pop("date", "2026-07-01"),
                        title=kw.pop("title", "Кров"), **kw)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def test_checkup_reminder_sent_once(session):
    from bot import jobs

    user = await _mk_user(session)
    today = dt.datetime.now(jobs.user_tz(user)).date()
    row = await _mk_checkup(
        session, user.id,
        next_due_date=(today - dt.timedelta(days=3)).isoformat(),  # overdue
    )
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    await jobs._checkup_reminder_for_user(ctx, session, user)
    ctx.bot.send_message.assert_called_once()
    assert "Кров" in ctx.bot.send_message.call_args.args[1]
    guard = await repository.get_state(session, user.id, checkup_reminders.guard_key(row))
    assert guard == "1"

    # a second tick never re-sends the same checkup's reminder
    ctx.bot.send_message.reset_mock()
    await jobs._checkup_reminder_for_user(ctx, session, user)
    ctx.bot.send_message.assert_not_called()


async def test_checkup_reminder_rearms_on_reschedule(session):
    """Bumping `next_due_date` on the same row is a deliberate reschedule — it must
    re-arm the reminder rather than staying silent forever (the v1 limitation this
    guard-key fix closes)."""
    from bot import jobs

    user = await _mk_user(session)
    today = dt.datetime.now(jobs.user_tz(user)).date()
    row = await _mk_checkup(
        session, user.id,
        next_due_date=(today - dt.timedelta(days=3)).isoformat(),
    )
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await jobs._checkup_reminder_for_user(ctx, session, user)
    ctx.bot.send_message.assert_called_once()

    # user reschedules the checkup to a new, still-due date on the SAME row
    ctx.bot.send_message.reset_mock()
    row.next_due_date = (today - dt.timedelta(days=1)).isoformat()
    await session.commit()
    await jobs._checkup_reminder_for_user(ctx, session, user)
    ctx.bot.send_message.assert_called_once()


async def test_checkup_reminder_skips_far_future(session):
    from bot import jobs

    user = await _mk_user(session)
    today = dt.datetime.now(jobs.user_tz(user)).date()
    await _mk_checkup(
        session, user.id,
        next_due_date=(today + dt.timedelta(days=90)).isoformat(),
    )
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await jobs._checkup_reminder_for_user(ctx, session, user)
    ctx.bot.send_message.assert_not_called()


async def test_checkup_reminder_skips_without_chat_id(session):
    from bot import jobs

    user = User(email="nochat@e.com", password_hash="h", is_approved=True, is_active=True)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    await _mk_checkup(session, user.id, next_due_date="2026-07-25")
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await jobs._checkup_reminder_for_user(ctx, session, user)
    ctx.bot.send_message.assert_not_called()


# --- /checkups command ------------------------------------------------------------

class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)


async def test_checkups_command_empty_state(session, monkeypatch):
    from bot import handlers as h

    user = await _mk_user(session)

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(h, "async_session_maker", maker)
    msg = _FakeMessage()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=user.telegram_chat_id),
                             message=msg)
    await h.checkups_cmd(update, SimpleNamespace(args=[]))
    assert "Аналізи" in msg.replies[-1]


async def test_checkups_command_lists_recent_and_upcoming(session, monkeypatch):
    from bot import handlers as h

    user = await _mk_user(session)
    await _mk_checkup(session, user.id, date="2026-07-01", title="Загальний аналіз крові",
                      next_due_date="2027-01-01")

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(h, "async_session_maker", maker)
    msg = _FakeMessage()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=user.telegram_chat_id),
                             message=msg)
    await h.checkups_cmd(update, SimpleNamespace(args=[]))
    text = msg.replies[-1]
    assert "Загальний аналіз крові" in text
    assert "2027-01-01" in text
