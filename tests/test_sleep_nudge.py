"""NF-16 evening sleep-debt nudge: the pure detector (app.sleepnudge) on synthetic series,
plus the once-a-evening job hook (bot.jobs._sleep_nudge_for_user)."""
import datetime as dt
from unittest.mock import patch

from app import sleepnudge
from app.db.models import PlannedWorkout, TrainingPlan, User
from bot import jobs as jobs_module

BASE = dt.date(2026, 6, 1)


def _row(i, **kw):
    d = (BASE + dt.timedelta(days=i)).isoformat()
    return {"date": d, "sleep_h": 7.5, "extra": {}, **kw}


def _healthy(n=30):
    return [_row(i) for i in range(n)]


# ---- has_sleep_debt ----------------------------------------------------------

def test_no_debt_on_stable_healthy_sleep():
    assert sleepnudge.has_sleep_debt(_healthy(30)) is False


def test_debt_when_recent_nights_below_personal_band():
    rows = _healthy(30)
    for i in (27, 28, 29):   # last 3 nights well below the personal band
        rows[i]["sleep_h"] = 4.5
    assert sleepnudge.has_sleep_debt(rows) is True


def test_no_debt_for_single_bad_night():
    rows = _healthy(30)
    rows[29]["sleep_h"] = 4.5   # one dip is a blip, not sustained
    assert sleepnudge.has_sleep_debt(rows) is False


def test_debt_from_sleep_need_gap_without_history():
    # not enough history for a personal band yet — but Garmin's own sleep_need vs actual
    # gap alone is a signal (a brand-new user isn't silent by default).
    rows = [_row(i) for i in range(5)]
    rows[-1]["sleep_h"] = 5.0
    rows[-1]["extra"] = {"sleep_need_h": 8.0}
    assert sleepnudge.has_sleep_debt(rows) is True


def test_no_debt_when_need_gap_small():
    rows = [_row(i) for i in range(5)]
    rows[-1]["sleep_h"] = 7.0
    rows[-1]["extra"] = {"sleep_need_h": 7.5}
    assert sleepnudge.has_sleep_debt(rows) is False


def test_no_debt_on_empty_history():
    assert sleepnudge.has_sleep_debt([]) is False


# ---- tomorrow_is_heavy --------------------------------------------------------

def test_tomorrow_heavy_true_for_key_types():
    assert sleepnudge.tomorrow_is_heavy(["tempo"]) is True
    assert sleepnudge.tomorrow_is_heavy(["easy", "long"]) is True


def test_tomorrow_heavy_false_for_easy_or_empty():
    assert sleepnudge.tomorrow_is_heavy(["easy", "rest"]) is False
    assert sleepnudge.tomorrow_is_heavy([]) is False


# ---- job hook: _sleep_nudge_for_user ------------------------------------------

async def _make_user(session, **kw):
    kw.setdefault("telegram_chat_id", 555)
    kw.setdefault("alerts_enabled", True)
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

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class _FakeCtx:
    def __init__(self):
        self.bot = _FakeBot()


TODAY = dt.date.today().isoformat()
TOMORROW = (dt.date.today() + dt.timedelta(days=1)).isoformat()


async def test_hook_silent_when_disabled(session):
    user = await _make_user(session, alerts_enabled=False)
    await _seed_plan(session, user.id, workouts=[
        dict(date=TOMORROW, type="tempo", status="planned")])
    ctx = _FakeCtx()
    await jobs_module._sleep_nudge_for_user(ctx, session, user, TODAY)
    assert ctx.bot.sent == []


async def test_hook_silent_without_heavy_tomorrow(session):
    user = await _make_user(session)
    await _seed_plan(session, user.id, workouts=[
        dict(date=TOMORROW, type="easy", status="planned")])
    ctx = _FakeCtx()
    with patch.object(jobs_module.baselines, "compute_baselines") as cb:
        await jobs_module._sleep_nudge_for_user(ctx, session, user, TODAY)
    cb.assert_not_called()   # never even reaches the sleep check — no heavy session
    assert ctx.bot.sent == []


async def test_hook_silent_without_sleep_debt(session):
    user = await _make_user(session)
    await _seed_plan(session, user.id, workouts=[
        dict(date=TOMORROW, type="tempo", status="planned")])
    ctx = _FakeCtx()
    with patch.object(jobs_module.sleepnudge, "has_sleep_debt", return_value=False):
        await jobs_module._sleep_nudge_for_user(ctx, session, user, TODAY)
    assert ctx.bot.sent == []


async def test_hook_sends_when_both_conditions_hold(session):
    user = await _make_user(session)
    await _seed_plan(session, user.id, workouts=[
        dict(date=TOMORROW, type="intervals", status="planned")])
    ctx = _FakeCtx()
    with patch.object(jobs_module.sleepnudge, "has_sleep_debt", return_value=True):
        await jobs_module._sleep_nudge_for_user(ctx, session, user, TODAY)
    assert len(ctx.bot.sent) == 1
    assert ctx.bot.sent[0][0] == 555
    assert "Завтра важка сесія" in ctx.bot.sent[0][1]


async def test_hook_guarded_once_per_evening(session):
    user = await _make_user(session)
    await _seed_plan(session, user.id, workouts=[
        dict(date=TOMORROW, type="long", status="planned")])
    with patch.object(jobs_module.sleepnudge, "has_sleep_debt", return_value=True):
        ctx1 = _FakeCtx()
        await jobs_module._sleep_nudge_for_user(ctx1, session, user, TODAY)
        assert len(ctx1.bot.sent) == 1

        ctx2 = _FakeCtx()
        await jobs_module._sleep_nudge_for_user(ctx2, session, user, TODAY)
        assert ctx2.bot.sent == []   # already sent today — guard holds


async def test_hook_silent_when_process_toggle_off(session, monkeypatch):
    user = await _make_user(session)
    await _seed_plan(session, user.id, workouts=[
        dict(date=TOMORROW, type="tempo", status="planned")])
    monkeypatch.setattr(jobs_module.settings, "SLEEP_NUDGE", False)
    ctx = _FakeCtx()
    with patch.object(jobs_module.sleepnudge, "has_sleep_debt", return_value=True):
        await jobs_module._sleep_nudge_for_user(ctx, session, user, TODAY)
    assert ctx.bot.sent == []
