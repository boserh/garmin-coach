"""NF-09: an actionable injury/health risk signal turned into a concrete ✅/❌ deload
proposal via the EP-02 adaptation engine, instead of a plain advisory the user must act
on manually."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import health as health_mod
from app import injury as injury_mod
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


def _edit(ops, summary="через сигнал ризику"):
    return PlanEdit(summary=summary, operations=ops, risky=False)


def _op(date, dist_km=4.0):
    return PlanOp(action="modify", date=date, dist_km=dist_km)


_HIGH = injury_mod.Assessment(
    level="high", score=6, history_days=60,
    signals=[injury_mod.Signal("acwr", 3, "ACWR 150% кілька днів")])
_NONE_INJURY = injury_mod.Assessment(level="none", score=0, history_days=60)

_HEALTH_ALERT = health_mod.HealthReport(
    level="alert", history_days=60,
    alerts=[health_mod.Alert("hrv_low", 2, "HRV нижче норми", "відпочинь")])
_HEALTH_NONE = health_mod.HealthReport(level="none", history_days=60)


async def _tomorrow_heavy(session, user_id):
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    await _seed_plan(session, user_id, workouts=[dict(date=tomorrow, type="tempo",
                                                       status="planned", dist_km=8.0)])
    return tomorrow


async def test_deload_fires_and_sets_guard(session):
    user = await _make_user(session)
    tomorrow = await _tomorrow_heavy(session, user.id)
    creds = SimpleNamespace(anthropic_key="k")
    edit = _edit([_op(tomorrow, dist_km=5.0)])
    with patch.object(jobs_module, "build_injury_assessment", return_value=_HIGH), \
         patch.object(jobs_module, "build_health_alerts", return_value=_HEALTH_NONE), \
         patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(
                          SimpleNamespace(id=1), edit))) as m:
        ctx = _FakeCtx()
        sent = await jobs_module._deload_check_for_user(
            ctx, session, user, creds, dt.date.today().isoformat())

    assert sent is True
    assert len(ctx.bot.sent) == 1
    m.assert_called_once()
    _, kwargs = m.call_args
    assert kwargs["trigger"] == "deload"
    assert kwargs["risk"]["injury"]["level"] == "high"
    assert "health" not in kwargs["risk"]

    guard = await repository.get_state(
        session, user.id, jobs_module.INJURY_WARNED_KEY)
    assert guard == dt.date.today().isoformat()


async def test_deload_folds_both_injury_and_health_signals(session):
    user = await _make_user(session)
    tomorrow = await _tomorrow_heavy(session, user.id)
    creds = SimpleNamespace(anthropic_key="k")
    edit = _edit([_op(tomorrow)])
    with patch.object(jobs_module, "build_injury_assessment", return_value=_HIGH), \
         patch.object(jobs_module, "build_health_alerts", return_value=_HEALTH_ALERT), \
         patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(SimpleNamespace(id=1), edit))) as m:
        await jobs_module._deload_check_for_user(
            _FakeCtx(), session, user, creds, dt.date.today().isoformat())

    _, kwargs = m.call_args
    assert kwargs["risk"]["injury"]["level"] == "high"
    assert kwargs["risk"]["health"][0]["kind"] == "hrv_low"


async def test_deload_skips_without_heavy_session(session):
    user = await _make_user(session)
    today = dt.date.today().isoformat()
    await _seed_plan(session, user.id, workouts=[dict(date=today, type="easy", status="planned")])
    creds = SimpleNamespace(anthropic_key="k")
    with patch.object(jobs_module, "build_injury_assessment", return_value=_HIGH), \
         patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        sent = await jobs_module._deload_check_for_user(_FakeCtx(), session, user, creds, today)
    assert sent is False
    m.assert_not_called()


async def test_deload_skips_when_not_actionable(session):
    user = await _make_user(session)
    await _tomorrow_heavy(session, user.id)
    creds = SimpleNamespace(anthropic_key="k")
    with patch.object(jobs_module, "build_injury_assessment", return_value=_NONE_INJURY), \
         patch.object(jobs_module, "build_health_alerts", return_value=_HEALTH_NONE), \
         patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        sent = await jobs_module._deload_check_for_user(
            _FakeCtx(), session, user, creds, dt.date.today().isoformat())
    assert sent is False
    m.assert_not_called()


async def test_deload_skips_within_guard(session):
    user = await _make_user(session)
    await _tomorrow_heavy(session, user.id)
    creds = SimpleNamespace(anthropic_key="k")
    today = dt.date.today().isoformat()
    await repository.set_state(session, user.id, jobs_module.INJURY_WARNED_KEY, today)
    await session.commit()
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        sent = await jobs_module._deload_check_for_user(_FakeCtx(), session, user, creds, today)
    assert sent is False
    m.assert_not_called()


async def test_deload_skips_with_pending_proposal(session):
    from bot.handlers import PENDING_ADAPT_KEY

    user = await _make_user(session)
    await _tomorrow_heavy(session, user.id)
    creds = SimpleNamespace(anthropic_key="k")
    today = dt.date.today().isoformat()
    await repository.set_state(session, user.id, PENDING_ADAPT_KEY, "[]")
    await session.commit()
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        sent = await jobs_module._deload_check_for_user(_FakeCtx(), session, user, creds, today)
    assert sent is False
    m.assert_not_called()


async def test_deload_skips_when_plan_adapt_disabled(session):
    user = await _make_user(session, plan_adapt_enabled=False)
    await _tomorrow_heavy(session, user.id)
    creds = SimpleNamespace(anthropic_key="k")
    today = dt.date.today().isoformat()
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        sent = await jobs_module._deload_check_for_user(_FakeCtx(), session, user, creds, today)
    assert sent is False
    m.assert_not_called()


async def test_deload_skips_empty_ops(session):
    """AC: adjust_level="off" makes run_plan_adaptation itself return (plan, None) with no
    Claude call — the deload hook treats that exactly like "nothing to propose", same as
    an empty operations list."""
    user = await _make_user(session)
    await _tomorrow_heavy(session, user.id)
    creds = SimpleNamespace(anthropic_key="k")
    today = dt.date.today().isoformat()
    with patch.object(jobs_module, "build_injury_assessment", return_value=_HIGH), \
         patch.object(jobs_module, "build_health_alerts", return_value=_HEALTH_NONE), \
         patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(SimpleNamespace(id=1), None))):
        sent = await jobs_module._deload_check_for_user(_FakeCtx(), session, user, creds, today)
    assert sent is False
    guard = await repository.get_state(session, user.id, jobs_module.INJURY_WARNED_KEY)
    assert guard is None


async def test_tick_skips_plain_advisories_when_deload_fires(session):
    """The wiring in _tick_for_user: a fired deload proposal replaces (not adds to) the
    plain injury/health advisories — same-day guard is shared via INJURY_WARNED_KEY."""
    user = await _make_user(session)
    tomorrow = await _tomorrow_heavy(session, user.id)
    creds = SimpleNamespace(anthropic_key="k")
    today = dt.date.today().isoformat()
    edit = _edit([_op(tomorrow)])
    ctx = _FakeCtx()

    with patch.object(jobs_module, "build_injury_assessment", return_value=_HIGH), \
         patch.object(jobs_module, "build_health_alerts", return_value=_HEALTH_NONE), \
         patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(SimpleNamespace(id=1), edit))), \
         patch.object(jobs_module, "run_injury_check", new=AsyncMock()) as injury_m, \
         patch.object(jobs_module, "run_health_alert", new=AsyncMock()) as health_m:
        deload_sent = await jobs_module._deload_check_for_user(ctx, session, user, creds, today)
        assert deload_sent is True

        # Mirrors the _tick_for_user branch: plain advisories are skipped once deload fired.
        if not deload_sent:
            await jobs_module._injury_check_for_user(ctx, session, user, creds, today)

    injury_m.assert_not_called()
    health_m.assert_not_called()
    assert len(ctx.bot.sent) == 1   # only the deload proposal went out
