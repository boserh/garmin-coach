"""Morning-job gating: only fire once today's recovery data (HRV + sleep) is in.
Also covers the activity-watch auto-analysis added for ST-04 and the shared per-user
job scaffold (eligible_users / for_each_user) added for CODE-04."""
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.db.models import User
from app.db.users import eligible_users
from app.garmin.schemas import DailySummary, Payload
from bot import jobs as jobs_module
from bot.jobs import (
    ACTIVITY_FRESH_DAYS,
    TZ,
    _activity_watch_for_user,
    _recovery_synced,
    for_each_user,
    user_tz,
)

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

    async def send_message(self, chat_id, text, **kwargs):
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


# --- CODE-04: shared per-user job scaffold ---------------------------------

async def _mk_user(session, **kw):
    defaults = dict(
        email=f"{kw.get('telegram_chat_id', 'x')}@e.com",
        password_hash="h",
        is_approved=True,
    )
    user = User(**{**defaults, **kw})
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_eligible_users_filters_active_and_approved(session):
    ok = await _mk_user(session, telegram_chat_id=1)
    await _mk_user(session, telegram_chat_id=2, is_active=False)
    await _mk_user(session, telegram_chat_id=3, is_approved=False)
    no_chat = await _mk_user(session, email="nochat@e.com")

    both = await eligible_users(session)
    assert {u.id for u in both} == {ok.id, no_chat.id}

    with_chat = await eligible_users(session, with_chat=True)
    assert {u.id for u in with_chat} == {ok.id}


async def test_for_each_user_isolates_failures(session, monkeypatch):
    u1 = await _mk_user(session, telegram_chat_id=1)
    u2 = await _mk_user(session, telegram_chat_id=2)
    u3 = await _mk_user(session, telegram_chat_id=3)

    @asynccontextmanager
    async def fake_maker():
        yield session

    monkeypatch.setattr(jobs_module, "async_session_maker", fake_maker)

    seen = []

    async def worker(_session, user):
        seen.append(user.id)
        if user.id == u2.id:
            raise RuntimeError("boom")

    await for_each_user(worker, with_chat=True, label="TEST")

    # u2 blew up but u1 and u3 still ran — one user's error never aborts the rest.
    assert seen == [u1.id, u2.id, u3.id]


# --- ST-14: per-user timezone -----------------------------------------------

def test_user_tz_reads_iana_string():
    user = SimpleNamespace(timezone="America/New_York")
    assert user_tz(user) == ZoneInfo("America/New_York")


def test_user_tz_falls_back_on_garbage():
    user = SimpleNamespace(timezone="Not/AZone")
    assert user_tz(user) == TZ


def test_user_tz_falls_back_on_missing():
    user = SimpleNamespace(timezone=None)
    assert user_tz(user) == TZ


# 10:00 UTC ≈ noon in Warsaw (summer, UTC+2) but 03:00 in Los Angeles (summer, UTC-7) —
# used to prove the morning-tick window check is evaluated in EACH user's own timezone,
# not the process default, without depending on the real wall-clock time of the test run.
_FIXED_UTC = dt.datetime(2026, 7, 22, 10, 0, tzinfo=dt.timezone.utc)


class _FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _FIXED_UTC if tz is None else _FIXED_UTC.astimezone(tz)


async def _tick_entered_runtime(monkeypatch, user) -> bool:
    monkeypatch.setattr(jobs_module.dt, "datetime", _FixedDateTime)
    entered = False

    @asynccontextmanager
    async def fake_runtime(*a, **kw):
        nonlocal entered
        entered = True
        yield None

    monkeypatch.setattr(jobs_module, "user_garmin_runtime", fake_runtime)
    await jobs_module._tick_for_user(_FakeCtx(), None, user)
    return entered


async def test_tick_runs_inside_users_local_window(monkeypatch):
    warsaw_user = SimpleNamespace(id=1, timezone="Europe/Warsaw", telegram_chat_id=1)
    assert await _tick_entered_runtime(monkeypatch, warsaw_user) is True


async def test_tick_skips_outside_users_local_window(monkeypatch):
    la_user = SimpleNamespace(id=2, timezone="America/Los_Angeles", telegram_chat_id=2)
    assert await _tick_entered_runtime(monkeypatch, la_user) is False


# --- Garmin rate-limit notification -----------------------------------------

async def test_tick_notifies_once_on_garmin_rate_limit(monkeypatch):
    """When Garmin keeps 429ing through every backoff retry, _api raises
    GarminRateLimited — the tick should DM the user once (not every 5-minute tick,
    now that CHECK_INTERVAL_MIN is tighter) and stay silent on a same-day repeat."""
    from app.garmin.client import GarminRateLimited

    user = SimpleNamespace(id=9, timezone="Europe/Warsaw", telegram_chat_id=9)
    monkeypatch.setattr(jobs_module.dt, "datetime", _FixedDateTime)

    @asynccontextmanager
    async def fake_runtime(*a, **kw):
        raise GarminRateLimited("/x")
        yield  # pragma: no cover — unreachable, keeps this a generator

    monkeypatch.setattr(jobs_module, "user_garmin_runtime", fake_runtime)

    state = {}

    async def fake_get_state(session, user_id, key):
        return state.get((user_id, key))

    async def fake_set_state(session, user_id, key, value):
        state[(user_id, key)] = value

    monkeypatch.setattr(jobs_module.repository, "get_state", fake_get_state)
    monkeypatch.setattr(jobs_module.repository, "set_state", fake_set_state)

    ctx = _FakeCtx()
    await jobs_module._tick_for_user(ctx, None, user)
    assert len(ctx.bot.sent) == 1
    chat_id, text = ctx.bot.sent[0]
    assert chat_id == 9
    assert "Garmin" in text

    # A second tick the same day must not resend the DM.
    await jobs_module._tick_for_user(ctx, None, user)
    assert len(ctx.bot.sent) == 1
