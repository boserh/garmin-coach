"""Adaptive plan hooks (EP-02): weekly review + morning nudge gating (Claude mocked)."""
import datetime as dt
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.db.models import PlannedWorkout, TrainingPlan, User
from app.garmin import repository
from app.garmin.schemas import PlanEdit, PlanOp
from bot import jobs as jobs_module


async def _make_user(session, **kw):
    kw.setdefault("telegram_chat_id", 555)
    kw.setdefault("plan_adapt_enabled", True)
    user = User(email=f"u{id(kw)}@x.com", password_hash="x", **kw)
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


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


class _FakeCtx:
    def __init__(self):
        self.bot = _FakeBot()


def _edit(ops, summary="через ACWR"):
    return PlanEdit(summary=summary, operations=ops, risky=False)


@asynccontextmanager
async def _fake_runtime(session_, user_):
    yield SimpleNamespace(anthropic_key="k", has_garmin=False)


# ---------- morning nudge ----------

async def test_morning_check_skips_without_heavy_session(session):
    user = await _make_user(session)
    today = dt.date.today().isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=today, type="easy", status="planned")])
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        await jobs_module._adapt_morning_check(
            _FakeCtx(), session, user, SimpleNamespace(anthropic_key="k"), today)
    m.assert_not_called()


async def test_morning_check_skips_when_readiness_ok(session):
    user = await _make_user(session)
    today = dt.date.today().isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=today, type="tempo", status="planned")])
    with patch.object(repository, "get_recent_extra",
                       new=AsyncMock(return_value={"readiness_score": 80})), \
         patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        await jobs_module._adapt_morning_check(
            _FakeCtx(), session, user, SimpleNamespace(anthropic_key="k"), today)
    m.assert_not_called()


async def test_morning_check_skips_disabled_toggle(session):
    user = await _make_user(session, plan_adapt_enabled=False)
    today = dt.date.today().isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=today, type="tempo", status="planned")])
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        await jobs_module._adapt_morning_check(
            _FakeCtx(), session, user, SimpleNamespace(anthropic_key="k"), today)
    m.assert_not_called()


async def test_morning_check_fires_once_and_sends_proposal(session):
    user = await _make_user(session)
    today = dt.date.today().isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=today, type="tempo", status="planned")])
    edit = _edit([PlanOp(action="modify", date=today, dist_km=3.0)])
    with patch.object(repository, "get_recent_extra",
                       new=AsyncMock(return_value={"readiness_score": 30})), \
         patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(SimpleNamespace(id=1), edit))) as m:
        ctx = _FakeCtx()
        creds = SimpleNamespace(anthropic_key="k")
        await jobs_module._adapt_morning_check(ctx, session, user, creds, today)
        # a second tick the same day (readiness still low) must not re-query Claude
        await jobs_module._adapt_morning_check(ctx, session, user, creds, today)
    assert m.await_count == 1
    assert len(ctx.bot.sent) == 1
    chat_id, text, _kb = ctx.bot.sent[0]
    assert chat_id == 555 and "через ACWR" in text


async def test_morning_check_silent_when_no_ops(session):
    user = await _make_user(session)
    today = dt.date.today().isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=today, type="long", status="planned")])
    with patch.object(repository, "get_recent_extra",
                       new=AsyncMock(return_value={"readiness_score": 20})), \
         patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(SimpleNamespace(id=1), _edit([])))):
        ctx = _FakeCtx()
        await jobs_module._adapt_morning_check(
            ctx, session, user, SimpleNamespace(anthropic_key="k"), today)
    assert ctx.bot.sent == []


# ---------- weekly review ----------

async def test_weekly_sends_proposal_when_ops_present(session):
    user = await _make_user(session)
    fut = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=fut, type="long", status="planned")])
    edit = _edit([PlanOp(action="modify", date=fut, dist_km=8.0)])
    with patch.object(jobs_module, "user_runtime", _fake_runtime), \
         patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(SimpleNamespace(id=1), edit))):
        ctx = _FakeCtx()
        await jobs_module._adapt_weekly_for_user(ctx, session, user)
    assert len(ctx.bot.sent) == 1


async def test_weekly_silent_when_no_ops(session):
    user = await _make_user(session)
    fut = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=fut, type="easy", status="planned")])
    with patch.object(jobs_module, "user_runtime", _fake_runtime), \
         patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(SimpleNamespace(id=1), _edit([])))):
        ctx = _FakeCtx()
        await jobs_module._adapt_weekly_for_user(ctx, session, user)
    assert ctx.bot.sent == []


async def test_weekly_skips_disabled_toggle(session):
    user = await _make_user(session, plan_adapt_enabled=False)
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        await jobs_module._adapt_weekly_for_user(_FakeCtx(), session, user)
    m.assert_not_called()


async def test_weekly_skips_without_active_plan(session):
    user = await _make_user(session)
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        await jobs_module._adapt_weekly_for_user(_FakeCtx(), session, user)
    m.assert_not_called()


# ---------- pending-proposal storage (bot_state, not user_data) ----------

async def test_send_adapt_proposal_stores_pending_in_bot_state(session):
    user = await _make_user(session)
    edit = _edit([PlanOp(action="modify", date="2026-07-10", dist_km=5.0)])
    await jobs_module._send_adapt_proposal(_FakeCtx(), session, user, edit)
    raw = await repository.get_state(session, user.id, jobs_module.PENDING_ADAPT_KEY)
    assert raw is not None
    data = json.loads(raw)
    assert data["ops"][0]["date"] == "2026-07-10"
    assert data["alt"] == []


async def test_risky_proposal_offers_three_buttons(session):
    from telegram import InlineKeyboardMarkup

    user = await _make_user(session)
    ops = [PlanOp(action="modify", date="2026-07-10", dist_km=8.0)]
    alt = [PlanOp(action="modify", date="2026-07-10", dist_km=6.0)]
    edit = PlanEdit(summary="ризиковано", operations=ops, risky=True,
                     alt_summary="безпечніше", alt_operations=alt)
    ctx = _FakeCtx()
    await jobs_module._send_adapt_proposal(ctx, session, user, edit)
    _chat_id, _text, kb = ctx.bot.sent[0]
    assert isinstance(kb, InlineKeyboardMarkup)
    assert len(kb.inline_keyboard) == 3
