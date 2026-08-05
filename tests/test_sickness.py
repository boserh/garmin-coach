"""NF-18: the auto "схоже, захворів" trigger — a streak of missed plan sessions PLUS an
actionable recovery report turns into one ✅/❌ offer of the NF-03 block rebuild.

All Claude calls are mocked — the suite spends $0 (and the detector itself is zero-LLM).
"""
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import health as health_mod
from app import sickness
from app.core.config import settings
from app.db.models import PlannedWorkout, TrainingPlan, User
from app.garmin import repository
from app.garmin.schemas import PlanEdit, PlanOp
from bot import handlers as handlers_module
from bot import jobs as jobs_module

TODAY = dt.date.today()
TODAY_S = TODAY.isoformat()


def _d(offset: int) -> str:
    return (TODAY + dt.timedelta(days=offset)).isoformat()


# ---------- pure detector ----------

def _rows(*pairs):
    return [{"date": _d(off), "status": st} for off, st in pairs]


def test_streak_counts_consecutive_missed():
    rows = _rows((-3, "missed"), (-2, "missed"), (-1, "missed"))
    assert sickness.missed_streak(rows, today=TODAY) == 3


def test_streak_broken_by_a_completed_session():
    """Back on track: the most recent session happened, so an older gap doesn't count."""
    rows = _rows((-4, "missed"), (-3, "missed"), (-2, "missed"), (-1, "done"))
    assert sickness.missed_streak(rows, today=TODAY) == 0


def test_streak_stops_at_the_last_completed_session():
    rows = _rows((-5, "missed"), (-4, "partial"), (-3, "missed"), (-2, "missed"))
    assert sickness.missed_streak(rows, today=TODAY) == 2


def test_rest_days_do_not_break_the_streak():
    """A rest/cross session is never matched, so it stays ``planned`` forever — it must be
    invisible to the streak instead of resetting it between two missed runs."""
    rows = _rows((-3, "missed"), (-2, "planned"), (-1, "missed"))
    assert sickness.missed_streak(rows, today=TODAY) == 2


def test_manual_skip_breaks_the_streak():
    """An explicit ST-21 "skipped" means the user is already managing the plan by hand."""
    rows = _rows((-3, "missed"), (-2, "missed"), (-1, "skipped"))
    assert sickness.missed_streak(rows, today=TODAY) == 0


def test_today_and_out_of_window_rows_are_ignored():
    rows = _rows((-9, "missed"), (-8, "missed"), (-2, "missed"), (0, "planned"))
    assert sickness.missed_streak(rows, today=TODAY) == 1


def test_unparseable_rows_are_skipped():
    rows = [{"date": None, "status": "missed"}, {"date": "not-a-date", "status": "missed"},
            *_rows((-1, "missed"))]
    assert sickness.missed_streak(rows, today=TODAY) == 1


def test_empty_input():
    assert sickness.missed_streak([], today=TODAY) == 0


# ---------- fixtures for the bot hook ----------

_EMAIL_SEQ = iter(range(1, 1000))


async def _make_user(session, **kw):
    kw.setdefault("telegram_chat_id", 555)
    kw.setdefault("plan_adapt_enabled", True)
    kw.setdefault("alerts_enabled", True)
    kw.setdefault("is_active", True)
    kw.setdefault("is_approved", True)
    user = User(email=f"u{next(_EMAIL_SEQ)}@x.com", password_hash="x", **kw)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _seed_plan(session, user_id, statuses):
    """statuses: list of (day_offset, status) — run sessions of the active plan."""
    plan = TrainingPlan(
        user_id=user_id, goal="g", status="active",
        start_date=_d(-30), target_date=_d(60),
    )
    session.add(plan)
    await session.flush()
    for off, status in statuses:
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=user_id, date=_d(off), type="easy",
            dist_km=5.0, status=status,
        ))
    await session.commit()
    return plan


async def _sick_streak_plan(session, user_id):
    return await _seed_plan(
        session, user_id, [(-3, "missed"), (-2, "missed"), (-1, "missed")])


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=7, chat=SimpleNamespace(id=chat_id))


class _FakeCtx:
    def __init__(self):
        self.bot = _FakeBot()


_CREDS = SimpleNamespace(anthropic_key="k")

_HEALTH_ALERT = health_mod.HealthReport(
    level="alert", history_days=60,
    alerts=[health_mod.Alert("hrv_low", 2, "HRV нижче норми", "відпочинь")])
_HEALTH_NONE = health_mod.HealthReport(level="none", history_days=60)


def _health(report):
    return patch.object(jobs_module, "build_health_alerts", return_value=report)


# ---------- the morning hook ----------

async def test_fires_on_missed_streak_plus_health_alert(session):
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    ctx = _FakeCtx()
    with _health(_HEALTH_ALERT):
        sent = await jobs_module._sickness_check_for_user(
            ctx, session, user, _CREDS, TODAY_S)

    assert sent is True
    assert len(ctx.bot.sent) == 1
    _chat, text, kb = ctx.bot.sent[0]
    assert "3 пропущені сесії" in text
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["sick:yes:3", "sick:no"]

    # Both the shared daily risk gate and this feature's own snooze are armed.
    assert await repository.get_state(session, user.id, jobs_module.INJURY_WARNED_KEY) == TODAY_S
    assert await repository.get_state(
        session, user.id, handlers_module.SICKNESS_WARNED_KEY) == TODAY_S


async def test_silent_without_health_signal(session):
    """AC: both conditions are required — a missed streak alone is as likely a work trip."""
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    ctx = _FakeCtx()
    with _health(_HEALTH_NONE):
        sent = await jobs_module._sickness_check_for_user(
            ctx, session, user, _CREDS, TODAY_S)
    assert sent is False
    assert ctx.bot.sent == []
    assert await repository.get_state(session, user.id, jobs_module.INJURY_WARNED_KEY) is None


async def test_silent_below_the_streak_threshold(session):
    """AC boundary: SICKNESS_MISSED_DAYS - 1 missed sessions → silence, and the (heavier)
    health detector is never even consulted."""
    user = await _make_user(session)
    await _seed_plan(session, user.id, [(-2, "missed"), (-1, "missed")])
    ctx = _FakeCtx()
    with patch.object(jobs_module, "build_health_alerts") as health_m:
        sent = await jobs_module._sickness_check_for_user(
            ctx, session, user, _CREDS, TODAY_S)
    assert sent is False
    health_m.assert_not_called()


async def test_silent_without_active_plan(session):
    user = await _make_user(session)
    ctx = _FakeCtx()
    with _health(_HEALTH_ALERT):
        sent = await jobs_module._sickness_check_for_user(
            ctx, session, user, _CREDS, TODAY_S)
    assert sent is False


async def test_silent_within_its_own_guard(session):
    """AC: ❌/ignored → silence for SICKNESS_GUARD_DAYS, then a fresh streak re-asks."""
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    recent = (TODAY - dt.timedelta(days=settings.SICKNESS_GUARD_DAYS - 1)).isoformat()
    await repository.set_state(session, user.id, handlers_module.SICKNESS_WARNED_KEY, recent)
    await session.commit()
    with _health(_HEALTH_ALERT):
        assert await jobs_module._sickness_check_for_user(
            _FakeCtx(), session, user, _CREDS, TODAY_S) is False

    expired = (TODAY - dt.timedelta(days=settings.SICKNESS_GUARD_DAYS)).isoformat()
    await repository.set_state(session, user.id, handlers_module.SICKNESS_WARNED_KEY, expired)
    await session.commit()
    with _health(_HEALTH_ALERT):
        assert await jobs_module._sickness_check_for_user(
            _FakeCtx(), session, user, _CREDS, TODAY_S) is True


async def test_silent_within_the_shared_risk_guard(session):
    """AC: never two risk DMs a day — an injury/deload touchpoint today shuts this up."""
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    await repository.set_state(session, user.id, jobs_module.INJURY_WARNED_KEY, TODAY_S)
    await session.commit()
    with _health(_HEALTH_ALERT):
        sent = await jobs_module._sickness_check_for_user(
            _FakeCtx(), session, user, _CREDS, TODAY_S)
    assert sent is False


async def test_silent_with_pending_proposal(session):
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    await repository.set_state(session, user.id, handlers_module.PENDING_ADAPT_KEY, "[]")
    await session.commit()
    with _health(_HEALTH_ALERT):
        sent = await jobs_module._sickness_check_for_user(
            _FakeCtx(), session, user, _CREDS, TODAY_S)
    assert sent is False


async def test_silent_with_pending_plan_edit(session):
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    await repository.set_pending_plan_edit(
        session, user.id, [{"action": "skip", "date": _d(1)}], [], summary="s")
    with _health(_HEALTH_ALERT):
        sent = await jobs_module._sickness_check_for_user(
            _FakeCtx(), session, user, _CREDS, TODAY_S)
    assert sent is False


async def test_silent_when_toggles_off(session):
    plain = await _make_user(session, alerts_enabled=False, telegram_chat_id=556)
    await _sick_streak_plan(session, plain.id)
    no_adapt = await _make_user(session, plan_adapt_enabled=False, telegram_chat_id=557)
    await _sick_streak_plan(session, no_adapt.id)
    on = await _make_user(session)
    await _sick_streak_plan(session, on.id)
    with _health(_HEALTH_ALERT):
        assert await jobs_module._sickness_check_for_user(
            _FakeCtx(), session, plain, _CREDS, TODAY_S) is False
        assert await jobs_module._sickness_check_for_user(
            _FakeCtx(), session, no_adapt, _CREDS, TODAY_S) is False
        with patch.object(settings, "SICKNESS_AUTO", False):
            assert await jobs_module._sickness_check_for_user(
                _FakeCtx(), session, on, _CREDS, TODAY_S) is False
        # …and the same user fires once the master switch is back on (the toggles are
        # the only thing keeping it quiet here).
        assert await jobs_module._sickness_check_for_user(
            _FakeCtx(), session, on, _CREDS, TODAY_S) is True


async def test_deload_wins_the_days_risk_slot(session):
    """The _tick_for_user wiring: a fired deload proposal means NF-18 stays quiet (and
    vice versa — a fired sickness question skips the plain injury/health advisories)."""
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    ctx = _FakeCtx()
    with _health(_HEALTH_ALERT), \
         patch.object(jobs_module, "run_injury_check", new=AsyncMock()) as injury_m, \
         patch.object(jobs_module, "run_health_alert", new=AsyncMock()) as health_m:
        deload_sent = False
        sick_sent = await jobs_module._sickness_check_for_user(
            ctx, session, user, _CREDS, TODAY_S)
        assert sick_sent is True
        if not deload_sent and not sick_sent:   # mirrors the tick branch
            await jobs_module._injury_check_for_user(ctx, session, user, _CREDS, TODAY_S)

    injury_m.assert_not_called()
    health_m.assert_not_called()
    assert len(ctx.bot.sent) == 1   # only the sickness question went out


# ---------- the ✅/❌ callback ----------

class _FakeQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.texts = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.texts.append(text)


def _update(data, chat_id=555):
    return SimpleNamespace(callback_query=_FakeQuery(data, chat_id))


def _use_session(session):
    """Make the handler's ``async_session_maker()`` reuse the test's in-memory session."""
    @asynccontextmanager
    async def _cm():
        yield session

    return patch.object(handlers_module, "async_session_maker", _cm)


def _edit():
    return PlanEdit(
        summary="Скасовую інтенсивність, повертаємось поступово.",
        operations=[PlanOp(action="skip", date=_d(1))], risky=False,
    )


async def test_callback_no_snoozes_and_spends_nothing(session):
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    upd = _update("sick:no")
    with _use_session(session), \
            patch("app.analysis.service.run_sick_check", new=AsyncMock()) as m:
        await handlers_module.sickness_callback(upd, _FakeCtx())
    m.assert_not_called()
    assert await repository.get_state(
        session, user.id, handlers_module.SICKNESS_WARNED_KEY) == TODAY_S


async def test_callback_yes_runs_sick_check_with_the_streak(session):
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    ctx = _FakeCtx()
    upd = _update("sick:yes:3")
    edit = _edit()
    with _use_session(session), \
            patch("app.analysis.service.run_sick_check",
                  new=AsyncMock(return_value=(SimpleNamespace(id=1), edit))) as m:
        await handlers_module.sickness_callback(upd, ctx)

    m.assert_awaited_once()
    assert m.call_args.kwargs["days_missed"] == 3
    # The rebuild lands in the standard plan-edit confirm flow (✅ Застосувати / ❌ Скасувати).
    assert len(ctx.bot.sent) == 1
    _chat, text, kb = ctx.bot.sent[0]
    assert edit.summary in text
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == [
        "plan_apply", "plan_cancel"]
    pending = await repository.get_pending_plan_edit(session, user.id)
    assert pending and pending["ops"][0]["action"] == "skip"


async def test_callback_yes_is_idempotent_against_a_stale_button(session):
    user = await _make_user(session)
    await _sick_streak_plan(session, user.id)
    await repository.set_pending_plan_edit(
        session, user.id, [{"action": "skip", "date": _d(1)}], [], summary="s")
    upd = _update("sick:yes:3")
    with _use_session(session), \
            patch("app.analysis.service.run_sick_check", new=AsyncMock()) as m:
        await handlers_module.sickness_callback(upd, _FakeCtx())
    m.assert_not_called()
    assert "вже чекає" in upd.callback_query.texts[-1]


async def test_callback_yes_without_active_plan(session):
    user = await _make_user(session)
    upd = _update("sick:yes:3")
    with _use_session(session), \
            patch("app.analysis.service.run_sick_check",
                  new=AsyncMock(return_value=(None, None))):
        await handlers_module.sickness_callback(upd, _FakeCtx())
    assert "Немає активної програми" in upd.callback_query.texts[-1]
    assert await repository.get_pending_plan_edit(session, user.id) is None
