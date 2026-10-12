"""The plan-move MCP server: one narrow write tool that proposes moving ONE already
planned session, through the exact same Telegram confirm/reject flow EP-02's adaptive
proposals use — never applies anything itself, never calls Claude.

``mcp`` is an opt-in extra (`pip install -e ".[mcp]"`), so this whole module skips
itself when it isn't installed (same pattern as test_mcp_notify.py).
"""
import datetime as dt

import pytest

pytest.importorskip("mcp")

from app import mcp_plan
from app.core.config import settings
from app.core.ratelimit import RateLimiter
from app.db import users
from app.db.models import PlannedWorkout, TrainingPlan


class _FakeBot:
    """Records what would have gone to Telegram; supports the ``async with Bot(...)``
    shape ``bot.jobs._send_adapt_proposal``'s caller uses."""

    def __init__(self):
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


class _FakeBotClass:
    """Stand-in for the ``telegram.Bot`` class — ``Bot(token=...)`` returns the same
    recorder every time so the test can inspect it after the call."""

    def __init__(self):
        self.instance = _FakeBot()

    def __call__(self, token=None):
        return self.instance


class _FakeMaker:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class _Token:
    def __init__(self, subject):
        self.subject = str(subject)


@pytest.fixture
def bot(monkeypatch):
    fake = _FakeBotClass()
    monkeypatch.setattr(mcp_plan, "Bot", fake)
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "t")
    return fake.instance


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    monkeypatch.setattr(mcp_plan, "_limiter", RateLimiter(20, 3600))


async def _seed_user_with_plan(session, *, chat_id=555, date="2026-07-01"):
    user = await users.create_user(
        session, email="mover@example.com", password_hash="x",
        is_approved=True,
    )
    user.telegram_chat_id = chat_id
    plan = TrainingPlan(user_id=user.id, goal="general", status="active")
    session.add(plan)
    await session.flush()
    w = PlannedWorkout(plan_id=plan.id, user_id=user.id, date=date, type="easy",
                        dist_km=8.0, description="легкий біг", status="planned")
    session.add(w)
    await session.commit()
    return user, plan, w


def _to_date(days: int) -> str:
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


async def test_tool_sends_a_confirm_proposal_and_applies_nothing(session, bot, monkeypatch):
    user, plan, w = await _seed_user_with_plan(session, date=_to_date(1))
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: _Token(user.id))

    got = await mcp_plan.propose_move(w.date, _to_date(3))

    assert got == {"proposed": True, "date": w.date, "to_date": _to_date(3)}
    assert bot.sent and bot.sent[0][0] == 555
    # nothing applied yet — the workout's own date is untouched until a Telegram tap
    await session.refresh(w)
    assert w.date != _to_date(3)


async def test_tool_refuses_a_date_with_no_planned_session(session, bot, monkeypatch):
    user, plan, w = await _seed_user_with_plan(session, date=_to_date(1))
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: _Token(user.id))

    with pytest.raises(ValueError, match="No PLANNED session"):
        await mcp_plan.propose_move(_to_date(9), _to_date(10))
    assert bot.sent == []


async def test_tool_refuses_an_already_done_session(session, bot, monkeypatch):
    user, plan, w = await _seed_user_with_plan(session, date=_to_date(1))
    w.status = "done"
    await session.commit()
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: _Token(user.id))

    with pytest.raises(ValueError, match="No PLANNED session"):
        await mcp_plan.propose_move(w.date, _to_date(3))
    assert bot.sent == []


async def test_tool_refuses_a_move_into_the_past(session, bot, monkeypatch):
    user, plan, w = await _seed_user_with_plan(session, date=_to_date(1))
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: _Token(user.id))

    with pytest.raises(ValueError, match="in the past"):
        await mcp_plan.propose_move(w.date, _to_date(-2))
    assert bot.sent == []


async def test_tool_refuses_the_same_date(session, bot, monkeypatch):
    user, plan, w = await _seed_user_with_plan(session, date=_to_date(1))
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: _Token(user.id))

    with pytest.raises(ValueError, match="nothing to move"):
        await mcp_plan.propose_move(w.date, w.date)
    assert bot.sent == []


async def test_tool_refuses_without_a_linked_telegram_chat(session, bot, monkeypatch):
    user, plan, w = await _seed_user_with_plan(session, date=_to_date(1), chat_id=None)
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: _Token(user.id))

    with pytest.raises(ValueError, match="Telegram"):
        await mcp_plan.propose_move(w.date, _to_date(3))
    assert bot.sent == []


async def test_tool_refuses_without_an_active_plan(session, bot, monkeypatch):
    user = await users.create_user(
        session, email="noplan@example.com", password_hash="x",
        is_approved=True,
    )
    user.telegram_chat_id = 1
    await session.commit()
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: _Token(user.id))

    with pytest.raises(ValueError, match="No active training plan"):
        await mcp_plan.propose_move(_to_date(1), _to_date(3))
    assert bot.sent == []


async def test_tool_rate_limits(session, bot, monkeypatch):
    user, plan, w = await _seed_user_with_plan(session, date=_to_date(1))
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: _Token(user.id))
    monkeypatch.setattr(mcp_plan, "_limiter", RateLimiter(1, 3600))

    await mcp_plan.propose_move(w.date, _to_date(3))
    with pytest.raises(ValueError, match="Rate limit"):
        await mcp_plan.propose_move(w.date, _to_date(4))
    assert len(bot.sent) == 1


# --- it lives on the existing coach server ------------------------------------------


def test_coach_server_carries_propose_move():
    """No own process/origin/connector: the "Bihun" connector the athlete already has
    serves it. Still nothing is applied without the Telegram ✅."""
    import anyio

    from app.mcp_server import build_server

    names = {t.name for t in anyio.run(build_server().list_tools)}
    assert "propose_move" in names and "get_training_plan" in names


def test_there_is_no_separate_plan_server():
    import app.mcp_oauth as oauth_mod

    assert not hasattr(mcp_plan, "main") and not hasattr(mcp_plan, "build_server")
    assert not hasattr(oauth_mod, "PLAN_SCOPE")
    assert not hasattr(settings, "MCP_PLAN_PUBLIC_URL")


async def test_stdio_uses_the_coach_servers_bound_user(session, bot, monkeypatch):
    """Over stdio the coach process binds one --email user; propose_move must act for
    that user, not demand a binding of its own."""
    from app import mcp_server

    user, plan, w = await _seed_user_with_plan(session, date=_to_date(1))
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: None)
    monkeypatch.setattr(mcp_server, "get_access_token", lambda: None)
    monkeypatch.setattr(mcp_server, "_user_id", user.id)
    got = await mcp_plan.propose_move(w.date, _to_date(2))
    assert got["proposed"] is True and bot.sent[0][0] == 555


async def test_propose_move_refuses_without_a_bot_token(session, bot, monkeypatch):
    """The token was checked at process start while this had a process of its own; on
    the coach server it is checked per call, so a missing one is a clear tool error."""
    user, plan, w = await _seed_user_with_plan(session, date=_to_date(1))
    monkeypatch.setattr(mcp_plan, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_plan, "get_access_token", lambda: _Token(user.id))
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", None)
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        await mcp_plan.propose_move(w.date, _to_date(2))
    assert bot.sent == []
