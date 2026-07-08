"""Weekly digest (EP-07): the run_digest service call (numbers computed by us, Claude
mocked; dedup cache) and the once-a-week guarded job hook."""
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.analysis import service
from app.analysis.service import CallStats, _week_volume_summary, run_digest
from app.db.models import ActivityRecord, ReportLog, User
from app.garmin import repository
from bot import jobs as jobs_module

U1 = 1


async def _seed_run(session, user_id, date, dist_km, activity_id):
    session.add(ActivityRecord(
        user_id=user_id, activity_id=activity_id, date=date,
        type="running", dist_km=dist_km,
    ))
    await session.commit()


async def _digest_logs(session):
    return (
        await session.execute(select(ReportLog).where(ReportLog.kind == "digest"))
    ).scalars().all()


# ---------- pure numbers ----------

def test_week_volume_summary_computes_deltas():
    this_wk, prev_wk = "2026-W28", "2026-W27"
    weekly = [
        {"week": prev_wk, "km": 20.0, "runs": 3, "longest_km": 8.0},
        {"week": this_wk, "km": 25.0, "runs": 4, "longest_km": 10.0},
    ]
    s = _week_volume_summary(weekly, this_wk, prev_wk)
    assert s["run_km"] == 25.0 and s["run_km_prev"] == 20.0
    assert s["delta_km"] == 5.0
    assert s["runs"] == 4 and s["runs_prev"] == 3
    assert s["longest_km"] == 10.0


def test_week_volume_summary_missing_weeks_read_as_zero():
    s = _week_volume_summary([], "2026-W28", "2026-W27")
    assert s == {
        "run_km": 0.0, "run_km_prev": 0.0, "delta_km": 0.0,
        "runs": 0, "runs_prev": 0, "longest_km": 0.0, "longest_km_prev": 0.0,
    }


# ---------- run_digest ----------

async def test_digest_none_when_no_history_and_no_plan(session):
    text = await run_digest(session, user_id=U1)
    assert text is None
    assert await _digest_logs(session) == []


async def test_digest_narrates_and_logs(session):
    today = dt.date.today().isoformat()
    await _seed_run(session, U1, today, 6.0, activity_id=1001)

    stats = CallStats(kind="digest", model=service.MODEL_DIGEST,
                      input_tokens=50, output_tokens=20, cost_usd=0.001)
    with patch.object(service, "digest_with_stats", return_value=("тижневий підсумок", stats)) as m:
        text = await run_digest(session, user_id=U1, api_key="k")

    assert text == "тижневий підсумок"
    m.assert_called_once()
    logs = await _digest_logs(session)
    assert len(logs) == 1 and logs[0].ok is True and logs[0].cached is False


async def test_digest_cache_hit_on_repeat(session):
    today = dt.date.today().isoformat()
    await _seed_run(session, U1, today, 6.0, activity_id=2001)

    stats = CallStats(kind="digest", model=service.MODEL_DIGEST)
    with patch.object(service, "digest_with_stats", return_value=("з кешу", stats)) as m:
        first = await run_digest(session, user_id=U1)
        second = await run_digest(session, user_id=U1)

    assert first == second == "з кешу"
    m.assert_called_once()  # second call served from the dedup cache, no new Claude call
    logs = await _digest_logs(session)
    assert len(logs) == 2 and logs[1].cached is True


async def test_digest_shortened_without_plan_still_sends(session):
    # Fitness snapshot present (via extra) but no active plan → still worth a digest.
    from app.db.models import DailyMetric
    today = dt.date.today().isoformat()
    session.add(DailyMetric(user_id=U1, date=today, extra={"vo2max": 46}))
    await session.commit()

    stats = CallStats(kind="digest", model=service.MODEL_DIGEST)
    with patch.object(service, "digest_with_stats", return_value=("коротка версія", stats)) as m:
        text = await run_digest(session, user_id=U1)

    assert text == "коротка версія"
    # no plan → the context carries no compliance/goal
    ctx = m.call_args.args[0]
    assert ctx["has_plan"] is False and ctx["compliance"] is None and ctx["goal"] is None


# ---------- job hook: once-a-week guard ----------

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class _FakeCtx:
    def __init__(self):
        self.bot = _FakeBot()


@asynccontextmanager
async def _fake_runtime(session_, user_):
    yield SimpleNamespace(anthropic_key="k", has_garmin=False)


async def _make_user(session, **kw):
    kw.setdefault("telegram_chat_id", 555)
    user = User(email=f"u{id(kw)}@x.com", password_hash="x", **kw)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_digest_job_sends_once_per_week(session):
    user = await _make_user(session)
    ctx = _FakeCtx()
    with patch.object(jobs_module, "user_runtime", _fake_runtime), \
         patch.object(jobs_module, "run_digest", new=AsyncMock(return_value="підсумок")):
        await jobs_module._digest_for_user(ctx, session, user)
        await jobs_module._digest_for_user(ctx, session, user)  # guarded — no re-send

    assert len(ctx.bot.sent) == 1
    chat_id, text = ctx.bot.sent[0]
    assert chat_id == 555 and "🗓 Тижневий підсумок" in text and "підсумок" in text
    guard = jobs_module.DIGEST_GUARD_PREFIX + dt.datetime.now(jobs_module.TZ).strftime("%G-W%V")
    assert await repository.get_state(session, user.id, guard) == "1"


async def test_digest_job_nothing_to_send_leaves_guard_unset(session):
    user = await _make_user(session)
    ctx = _FakeCtx()
    with patch.object(jobs_module, "user_runtime", _fake_runtime), \
         patch.object(jobs_module, "run_digest", new=AsyncMock(return_value=None)):
        await jobs_module._digest_for_user(ctx, session, user)

    assert ctx.bot.sent == []
    guard = jobs_module.DIGEST_GUARD_PREFIX + dt.datetime.now(jobs_module.TZ).strftime("%G-W%V")
    assert await repository.get_state(session, user.id, guard) is None


async def test_force_digest_bypasses_guard(session):
    user = await _make_user(session)
    ctx = _FakeCtx()
    guard = jobs_module.DIGEST_GUARD_PREFIX + dt.datetime.now(jobs_module.TZ).strftime("%G-W%V")
    await repository.set_state(session, user.id, guard, "1")  # already sent this week

    with patch.object(jobs_module, "user_runtime", _fake_runtime), \
         patch.object(jobs_module, "run_digest", new=AsyncMock(return_value="ручний")):
        await jobs_module.force_digest_for_user(ctx, session, user)

    assert len(ctx.bot.sent) == 1
    assert "🧪 [тест] " in ctx.bot.sent[0][1]
