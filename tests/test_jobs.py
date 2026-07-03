"""Morning-job gating: only fire once today's recovery data (HRV + sleep) is in.
Also covers the activity-watch auto-analysis added for ST-04."""
import datetime as dt
from types import SimpleNamespace

from app.garmin.schemas import DailySummary, Payload
from bot import jobs as jobs_module
from bot.jobs import ACTIVITY_FRESH_DAYS, _activity_watch_for_user, _recovery_synced

TODAY = "2026-06-24"


def _payload(today_row: DailySummary) -> Payload:
    return Payload(
        generated="2026-06-24T08:00:00", window_days=3, synced_today=True,
        last_data_date=TODAY, daily=[today_row], recent_activities=[], planned_runs=[],
    )


def test_recovery_synced_requires_hrv_and_sleep():
    full = _payload(DailySummary(date=TODAY, hrv_avg=60, sleep_score=80, has_data=True))
    assert _recovery_synced(full, TODAY) is True


def test_recovery_not_synced_with_stress_only():
    # Garmin synced stress early but not HRV/sleep — too loose to fire the morning report
    stress_only = _payload(DailySummary(date=TODAY, stress_avg=25, has_data=True))
    assert _recovery_synced(stress_only, TODAY) is False


def test_recovery_not_synced_with_hrv_but_no_sleep():
    partial = _payload(DailySummary(date=TODAY, hrv_avg=60, has_data=True))
    assert _recovery_synced(partial, TODAY) is False


def test_recovery_not_synced_when_no_today_row():
    other = _payload(DailySummary(date="2026-06-23", hrv_avg=60, sleep_score=80, has_data=True))
    assert _recovery_synced(other, TODAY) is False


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeCtx:
    def __init__(self):
        self.bot = _FakeBot()


def _act(id_, date, type_="running", dist_km=5.0):
    return SimpleNamespace(id=id_, date=date, type=type_, dist_km=dist_km)


_USER = SimpleNamespace(id=1, telegram_chat_id=555)
_CREDS = SimpleNamespace(anthropic_key="k")


async def test_activity_watch_analyzes_fresh_run(monkeypatch):
    act = _act(1, dt.date.today().isoformat())

    async def fake_analyze(session, activity, *, user_id, api_key):
        return "аналіз пробіжки"

    monkeypatch.setattr(jobs_module, "run_activity_analysis", fake_analyze)
    ctx = _FakeCtx()

    await _activity_watch_for_user(ctx, None, _USER, _CREDS, [act])

    assert len(ctx.bot.sent) == 1
    chat_id, text = ctx.bot.sent[0]
    assert chat_id == 555
    assert "аналіз пробіжки" in text


async def test_activity_watch_skips_old_activity(monkeypatch):
    stale = (dt.date.today() - dt.timedelta(days=ACTIVITY_FRESH_DAYS + 1)).isoformat()
    act = _act(2, stale)
    called = False

    async def fake_analyze(*a, **kw):
        nonlocal called
        called = True
        return "x"

    monkeypatch.setattr(jobs_module, "run_activity_analysis", fake_analyze)
    ctx = _FakeCtx()

    await _activity_watch_for_user(ctx, None, _USER, _CREDS, [act])

    assert not called
    assert ctx.bot.sent == []


async def test_activity_watch_skips_non_run_type(monkeypatch):
    act = _act(3, dt.date.today().isoformat(), type_="strength_training")
    called = False

    async def fake_analyze(*a, **kw):
        nonlocal called
        called = True
        return "x"

    monkeypatch.setattr(jobs_module, "run_activity_analysis", fake_analyze)
    ctx = _FakeCtx()

    await _activity_watch_for_user(ctx, None, _USER, _CREDS, [act])

    assert not called
    assert ctx.bot.sent == []


async def test_activity_watch_continues_after_one_failure(monkeypatch):
    today = dt.date.today().isoformat()
    act1, act2 = _act(4, today), _act(5, today)
    calls = []

    async def fake_analyze(session, activity, *, user_id, api_key):
        calls.append(activity.id)
        if activity.id == 4:
            raise RuntimeError("boom")
        return "ok"

    monkeypatch.setattr(jobs_module, "run_activity_analysis", fake_analyze)
    ctx = _FakeCtx()

    await _activity_watch_for_user(ctx, None, _USER, _CREDS, [act1, act2])

    assert calls == [4, 5]
    assert len(ctx.bot.sent) == 1  # only act2 (id=5) produced a message
