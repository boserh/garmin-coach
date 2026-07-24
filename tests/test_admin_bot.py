"""The split system/admin bot (bot.admin_main): only /deploy + /test_* are wired, the
main bot no longer carries them, and every update is gated to the first user."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop, CommandHandler, TypeHandler

import bot.admin_main as admin_main
import bot.main as bot_main
from app.db.models import User


class _FakeApp:
    """Records handler registrations without building a real PTB Application."""

    def __init__(self):
        self.handlers = []      # (group, handler)
        self.error_handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((group, handler))

    def add_error_handler(self, handler):
        self.error_handlers.append(handler)

    def command_names(self):
        names = set()
        for _, h in self.handlers:
            if isinstance(h, CommandHandler):
                names |= set(h.commands)
        return names


# --- handler wiring -----------------------------------------------------------

def test_admin_bot_wires_only_system_commands():
    app = _FakeApp()
    admin_main.register_admin_handlers(app)

    assert app.command_names() == {
        "deploy", "test_on", "test_off", "test_morning", "test_digest",
    }
    # owner gate registered ahead of everything (group -1) as a TypeHandler
    assert any(g == -1 and isinstance(h, TypeHandler) for g, h in app.handlers)
    assert app.error_handlers  # on_error wired


def test_main_bot_no_longer_carries_system_commands():
    app = _FakeApp()
    bot_main.register_handlers(app)

    names = app.command_names()
    assert not (names & {"deploy", "test_on", "test_off", "test_morning", "test_digest"})
    # …but the product commands are all still there
    assert {"report", "ask", "plan", "race", "help"} <= names
    assert app.error_handlers


# --- owner-only gate ----------------------------------------------------------

async def _mk_user(session, chat_id):
    u = User(email=f"{chat_id}@e.com", password_hash="h", is_approved=True,
             is_active=True, telegram_chat_id=chat_id)
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest.fixture
def _admin_session(session, monkeypatch):
    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(admin_main, "async_session_maker", maker)
    return session


class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


def _update(chat_id, msg):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=msg,
        callback_query=None,
    )


async def test_owner_gate_allows_first_user(_admin_session):
    first = await _mk_user(_admin_session, chat_id=111)
    await _mk_user(_admin_session, chat_id=222)  # a later user
    msg = _FakeMessage()

    # No ApplicationHandlerStop → the real handler would run.
    await admin_main._owner_only(_update(first.telegram_chat_id, msg), None)
    assert msg.replies == []


async def test_owner_gate_denies_second_user(_admin_session):
    await _mk_user(_admin_session, chat_id=111)          # the owner
    second = await _mk_user(_admin_session, chat_id=222)
    msg = _FakeMessage()

    with pytest.raises(ApplicationHandlerStop):
        await admin_main._owner_only(_update(second.telegram_chat_id, msg), None)
    assert "власника" in msg.replies[-1]


async def test_owner_gate_denies_unknown_chat(_admin_session):
    await _mk_user(_admin_session, chat_id=111)
    msg = _FakeMessage()

    with pytest.raises(ApplicationHandlerStop):
        await admin_main._owner_only(_update(999, msg), None)
    assert "власника" in msg.replies[-1]
