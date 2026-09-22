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


# --- separation from the coach/notify servers ----------------------------------------


def test_all_three_servers_require_disjoint_scopes():
    from app.mcp_http import auth_kwargs
    from app.mcp_oauth import NOTIFY_SCOPE, PLAN_SCOPE, SCOPE

    coach = auth_kwargs("https://mcp.example.com", SCOPE)["auth"].required_scopes
    notify = auth_kwargs("https://mon.example.com", NOTIFY_SCOPE)["auth"].required_scopes
    plan = auth_kwargs("https://plan.example.com", PLAN_SCOPE)["auth"].required_scopes
    assert coach == [SCOPE] and notify == [NOTIFY_SCOPE] and plan == [PLAN_SCOPE]
    assert not (set(coach) & set(notify)) and not (set(coach) & set(plan))
    assert not (set(notify) & set(plan))


def test_plan_server_exposes_only_the_move_tool():
    assert [fn.__name__ for fn in mcp_plan._TOOLS] == ["propose_move"]


def test_plan_server_refuses_to_start_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", None)
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        mcp_plan.main(["--email", "x@example.com"])


def test_plan_http_refuses_to_start_without_a_public_url(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(settings, "MCP_PLAN_PUBLIC_URL", None)
    with pytest.raises(SystemExit, match="MCP_PLAN_PUBLIC_URL"):
        mcp_plan.main(["--transport", "http"])
