"""`python -m app.cli trigger-plan-adapt`: manually re-run the weekly EP-02 review
(console-triggered) — same call plan_adapt_job makes, delivered as the usual ✅/❌
Telegram proposal via a standalone telegram.Bot (no python-telegram-bot Application)."""
from contextlib import asynccontextmanager

import pytest
from cryptography.fernet import Fernet

from app import cli
from app.core import crypto
from app.db.models import PlannedWorkout, TrainingPlan, User
from app.garmin.schemas import PlanEdit, PlanOp


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet", None)


@pytest.fixture
def _cli_session(session, monkeypatch):
    """Route app.cli's async_session_maker/init_db to the test in-memory session."""
    @asynccontextmanager
    async def maker():
        yield session

    async def _noop_init_db():
        return None

    monkeypatch.setattr(cli, "async_session_maker", maker)
    monkeypatch.setattr(cli, "init_db", _noop_init_db)
    return session


class _FakeBot:
    def __init__(self, token=None):
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


async def _mk_user(session, key, **kw):
    kw.setdefault("telegram_chat_id", 555)
    kw.setdefault("anthropic_key_enc", crypto.encrypt("sk-ant-test"))
    user = User(email=f"u{id(kw)}@x.com", password_hash="h", **kw)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _seed_plan(session, user_id, *, workouts):
    plan = TrainingPlan(
        user_id=user_id, goal="g", status="active",
        start_date="2026-06-01", target_date="2026-09-01",
    )
    session.add(plan)
    await session.flush()
    for w in workouts:
        session.add(PlannedWorkout(plan_id=plan.id, user_id=user_id, **w))
    await session.commit()
    return plan


async def test_no_such_user(_cli_session, capsys):
    # A4: the shared cli_user preamble raises _UserNotFound for an unknown email; main()'s
    # _run wrapper turns that into the uniform "User <email> not found." message + exit 1.
    with pytest.raises(cli._UserNotFound):
        await cli._trigger_plan_adapt("nobody@x.com")


async def test_no_telegram_chat_id(_cli_session, key, capsys):
    user = await _mk_user(_cli_session, key, telegram_chat_id=None)
    rc = await cli._trigger_plan_adapt(user.email)
    assert rc == 1
    assert "telegram_chat_id" in capsys.readouterr().out


async def test_no_active_plan(_cli_session, key, capsys):
    user = await _mk_user(_cli_session, key)
    rc = await cli._trigger_plan_adapt(user.email)
    assert rc == 1
    assert "No active plan" in capsys.readouterr().out


async def test_sends_proposal_when_ops_present(_cli_session, key, monkeypatch, capsys):
    user = await _mk_user(_cli_session, key)
    plan = await _seed_plan(_cli_session, user.id, workouts=[
        dict(date="2026-07-10", type="long", dist_km=12.0, status="planned"),
    ])
    edit = PlanEdit(
        summary="через ACWR",
        operations=[PlanOp(action="modify", date="2026-07-10", dist_km=9.0)],
    )

    async def _fake_adapt(session, *, user_id, api_key, trigger):
        assert user_id == user.id
        assert api_key == "sk-ant-test"
        return plan, edit

    from app.analysis import service as analysis_service
    monkeypatch.setattr(analysis_service, "run_plan_adaptation", _fake_adapt)

    fake_bot = _FakeBot()
    monkeypatch.setattr("telegram.Bot", lambda token=None: fake_bot)

    rc = await cli._trigger_plan_adapt(user.email)
    assert rc == 0
    assert "Proposal sent" in capsys.readouterr().out
    assert len(fake_bot.sent) == 1
    chat_id, text, kb = fake_bot.sent[0]
    assert chat_id == user.telegram_chat_id
    assert "12 → 9 км" in text
    assert kb is not None


async def test_silent_when_plan_is_fine(_cli_session, key, monkeypatch, capsys):
    user = await _mk_user(_cli_session, key)
    plan = await _seed_plan(_cli_session, user.id, workouts=[
        dict(date="2026-07-10", type="easy", dist_km=5.0, status="planned"),
    ])
    edit = PlanEdit(summary="все ок", operations=[])

    async def _fake_adapt(session, *, user_id, api_key, trigger):
        return plan, edit

    from app.analysis import service as analysis_service
    monkeypatch.setattr(analysis_service, "run_plan_adaptation", _fake_adapt)

    def _boom(token=None):
        raise AssertionError("Bot should not be constructed when there are no ops")

    monkeypatch.setattr("telegram.Bot", _boom)

    rc = await cli._trigger_plan_adapt(user.email)
    assert rc == 0
    assert "nothing to propose" in capsys.readouterr().out.lower()
