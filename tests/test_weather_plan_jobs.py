"""Weather-aware planning job (EP-13): gating, silence-on-no-conflict, and the
double-ping guard (Claude + weather API mocked, no network)."""
import datetime as dt
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
    kw.setdefault("latitude", 51.1)
    kw.setdefault("longitude", 17.03)
    user = User(email=f"u{id(kw)}@x.com", password_hash="x", **kw)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _seed_plan(session, user_id, *, workouts):
    plan = TrainingPlan(user_id=user_id, goal="g", status="active", start_date="2026-06-01")
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


@asynccontextmanager
async def _fake_runtime(session_, user_):
    yield SimpleNamespace(anthropic_key="k", has_garmin=False)


def _hot_week(hot_date):
    return [{"date": hot_date, "t_max_c": 34, "feels_max_c": 36,
             "precip_prob_pct": 5, "wind_max_kmh": 12, "code": 0}]


async def test_skips_without_location(session):
    user = await _make_user(session, latitude=None, longitude=None)
    with patch.object(jobs_module.weather, "fetch_forecast_week") as fw:
        await jobs_module._weather_plan_for_user(_FakeCtx(), session, user)
    fw.assert_not_called()


async def test_skips_when_disabled(session):
    user = await _make_user(session, plan_adapt_enabled=False)
    with patch.object(jobs_module.weather, "fetch_forecast_week") as fw:
        await jobs_module._weather_plan_for_user(_FakeCtx(), session, user)
    fw.assert_not_called()


async def test_silent_when_no_conflict_no_claude(session):
    user = await _make_user(session)
    fut = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=fut, type="tempo", status="planned")])
    # calm forecast → no conflict → zero Claude calls, nothing sent
    calm = [{"date": fut, "t_max_c": 22, "feels_max_c": 23,
             "precip_prob_pct": 10, "wind_max_kmh": 8, "code": 1}]
    with patch.object(jobs_module.weather, "fetch_forecast_week",
                      return_value=calm), \
         patch.object(jobs_module, "run_weather_plan_check", new=AsyncMock()) as m:
        ctx = _FakeCtx()
        await jobs_module._weather_plan_for_user(ctx, session, user)
    m.assert_not_called()
    assert ctx.bot.sent == []


async def test_conflict_sends_proposal(session):
    user = await _make_user(session)
    fut = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    later = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    await _seed_plan(session, user.id,
                     workouts=[dict(date=fut, type="intervals", status="planned")])
    edit = PlanEdit(summary="прогноз на зараз: спека — переношу",
                    operations=[PlanOp(action="move", date=fut, to_date=later)], risky=False)
    with patch.object(jobs_module.weather, "fetch_forecast_week",
                      return_value=_hot_week(fut)), \
         patch.object(jobs_module, "run_weather_plan_check",
                      new=AsyncMock(return_value=(SimpleNamespace(id=1), edit))) as m, \
         patch.object(jobs_module, "user_runtime", _fake_runtime):
        ctx = _FakeCtx()
        await jobs_module._weather_plan_for_user(ctx, session, user)
    m.assert_awaited_once()
    assert len(ctx.bot.sent) == 1
    assert "прогноз на зараз" in ctx.bot.sent[0][1]


async def test_yields_to_pending_proposal(session):
    user = await _make_user(session)
    fut = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=fut, type="long", status="planned")])
    # an adaptation proposal is already waiting → weather must not query Claude / re-ping
    await repository.set_state(session, user.id, jobs_module.PENDING_ADAPT_KEY, '{"ops": []}')
    with patch.object(jobs_module.weather, "fetch_forecast_week") as fw, \
         patch.object(jobs_module, "run_weather_plan_check", new=AsyncMock()) as m:
        await jobs_module._weather_plan_for_user(_FakeCtx(), session, user)
    fw.assert_not_called()
    m.assert_not_called()


async def test_silent_when_no_ops(session):
    user = await _make_user(session)
    fut = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=fut, type="tempo", status="planned")])
    edit = PlanEdit(summary="погода не критична", operations=[], risky=False)
    with patch.object(jobs_module.weather, "fetch_forecast_week",
                      return_value=_hot_week(fut)), \
         patch.object(jobs_module, "run_weather_plan_check",
                      new=AsyncMock(return_value=(SimpleNamespace(id=1), edit))), \
         patch.object(jobs_module, "user_runtime", _fake_runtime):
        ctx = _FakeCtx()
        await jobs_module._weather_plan_for_user(ctx, session, user)
    assert ctx.bot.sent == []
