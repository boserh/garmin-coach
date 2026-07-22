"""EP-05 race pack: the pure `app.race` helpers, `run_race_plan` (None without a target,
narrate+log, cache hit on repeat), `repository.get_last_report_of_kind`, and the daily
`_race_pack_for_user` auto-trigger (bot/jobs.py)."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app import race
from app.analysis import reports, service
from app.analysis.service import CallStats, _race_cache_key, run_race_plan
from app.db.models import ReportLog, TrainingPlan, User
from app.garmin import repository

U1 = 1


def _plan(**kw):
    defaults = dict(user_id=U1, goal="first_10k", goal_label="Перші 10 км",
                     status="active", start_date="2026-06-01")
    defaults.update(kw)
    return TrainingPlan(**defaults)


# --- pure helpers ---------------------------------------------------------------

def test_distance_for_goal():
    assert race.distance_for_goal("first_5k") == 5.0
    assert race.distance_for_goal("faster_5k") == 5.0
    assert race.distance_for_goal("first_10k") == 10.0
    assert race.distance_for_goal("first_half") == 21.0975
    assert race.distance_for_goal("general") is None
    assert race.distance_for_goal(None) is None


def test_has_target():
    assert race.has_target(_plan(target_date="2026-08-01")) is True
    assert race.has_target(_plan(target_date=None)) is False           # no date
    assert race.has_target(_plan(goal="general", target_date="2026-08-01")) is False  # no dist
    assert race.has_target(None) is False


def test_days_to_target():
    today = dt.date(2026, 7, 22)
    assert race.days_to_target("2026-07-29", today) == 7
    assert race.days_to_target("2026-07-15", today) == -7
    assert race.days_to_target(None, today) is None
    assert race.days_to_target("garbage", today) is None


def test_build_context_shapes_fields():
    plan = _plan(target_date="2026-08-01")
    ctx = race.build_context(
        plan, {"vo2max": 50}, [{"date": "2026-07-30", "type": "easy"}], {"feels_max_c": 30},
    )
    assert ctx["goal"] == "first_10k"
    assert ctx["target_dist_km"] == 10.0
    assert ctx["target_metric"] == "race_10k_s"
    assert ctx["fitness"] == {"vo2max": 50}
    assert ctx["weather"] == {"feels_max_c": 30}


# --- repository -------------------------------------------------------------

async def test_get_last_report_of_kind(session):
    session.add_all([
        ReportLog(user_id=U1, kind="report", model="m", ok=True, report_text="daily"),
        ReportLog(user_id=U1, kind="race", model="m", ok=True, report_text="pack v1"),
        ReportLog(user_id=U1, kind="race", model="m", ok=False, report_text=None, error="boom"),
    ])
    await session.commit()
    result = await repository.get_last_report_of_kind(session, U1, "race")
    assert result is not None
    text, date = result
    assert text == "pack v1"


async def test_get_last_report_of_kind_none_when_missing(session):
    assert await repository.get_last_report_of_kind(session, U1, "race") is None


# --- run_race_plan service ---------------------------------------------------

async def _race_logs(session):
    return list((await session.execute(
        select(ReportLog).where(ReportLog.kind == "race")
    )).scalars().all())


async def test_run_race_plan_none_without_active_plan(session):
    text = await run_race_plan(session, user_id=U1)
    assert text is None
    assert await _race_logs(session) == []


async def test_run_race_plan_none_for_open_ended_goal(session):
    session.add(_plan(goal="general", target_date=None))
    await session.commit()
    text = await run_race_plan(session, user_id=U1)
    assert text is None


async def test_run_race_plan_narrates_and_logs(session):
    today = dt.date.today()
    plan = _plan(target_date=(today + dt.timedelta(days=6)).isoformat())
    session.add(plan)
    await session.commit()

    stats = CallStats(kind="race", model=service.MODEL_RACE,
                      input_tokens=80, output_tokens=60, cost_usd=0.02)
    with patch.object(reports, "race_plan_with_stats",
                      return_value=("твій race pack", stats)) as m:
        text = await run_race_plan(session, user_id=U1, api_key="k")

    assert text == "твій race pack"
    m.assert_called_once()
    logs = await _race_logs(session)
    assert len(logs) == 1 and logs[0].ok is True and logs[0].cached is False
    assert logs[0].question == f"race:{plan.id}"


async def test_run_race_plan_cache_hit_on_repeat(session):
    today = dt.date.today()
    session.add(_plan(target_date=(today + dt.timedelta(days=6)).isoformat()))
    await session.commit()

    stats = CallStats(kind="race", model=service.MODEL_RACE)
    with patch.object(reports, "race_plan_with_stats", return_value=("з кешу", stats)) as m:
        first = await run_race_plan(session, user_id=U1)
        second = await run_race_plan(session, user_id=U1)

    assert first == second == "з кешу"
    m.assert_called_once()
    logs = await _race_logs(session)
    assert len(logs) == 2 and logs[1].cached is True


def test_race_cache_key_stable_and_sensitive():
    ctx = {"goal": "first_10k", "target_date": "2026-08-01", "target_dist_km": 10.0,
           "fitness": None, "recent_sessions": [], "weather": None}
    k1 = _race_cache_key(ctx, "m")
    k2 = _race_cache_key(dict(ctx), "m")
    assert k1 == k2
    k3 = _race_cache_key({**ctx, "weather": {"feels_max_c": 32}}, "m")
    assert k1 != k3


# --- bot/jobs.py auto-trigger -------------------------------------------------

async def test_race_pack_job_sends_once_at_trigger_day(session):
    from bot import jobs

    today = dt.date.today()
    user = User(id=U1, email="a@b.c", password_hash="x", telegram_chat_id=555)
    session.add(user)
    session.add(_plan(target_date=(today + dt.timedelta(days=race.TRIGGER_DAYS)).isoformat()))
    await session.commit()

    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    with patch.object(jobs, "run_race_plan", new=AsyncMock(return_value="pack text")), \
         patch.object(jobs, "load_credentials",
                      return_value=SimpleNamespace(anthropic_key="k")):
        await jobs._race_pack_for_user(ctx, session, user)
        await jobs._race_pack_for_user(ctx, session, user)  # second tick same day: no resend

    ctx.bot.send_message.assert_called_once()
    assert "pack text" in ctx.bot.send_message.call_args.args[1]


async def test_race_pack_job_skips_outside_trigger_day(session):
    from bot import jobs

    today = dt.date.today()
    user = User(id=U1, email="a@b.c", password_hash="x", telegram_chat_id=555)
    session.add(user)
    session.add(_plan(target_date=(today + dt.timedelta(days=3)).isoformat()))
    await session.commit()

    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    with patch.object(jobs, "run_race_plan", new=AsyncMock(return_value="pack text")):
        await jobs._race_pack_for_user(ctx, session, user)

    ctx.bot.send_message.assert_not_called()


async def test_race_pack_job_skips_without_chat_id(session):
    from bot import jobs

    today = dt.date.today()
    user = User(id=U1, email="a@b.c", password_hash="x", telegram_chat_id=None)
    session.add(user)
    session.add(_plan(target_date=(today + dt.timedelta(days=race.TRIGGER_DAYS)).isoformat()))
    await session.commit()

    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    with patch.object(jobs, "run_race_plan", new=AsyncMock(return_value="pack text")):
        await jobs._race_pack_for_user(ctx, session, user)

    ctx.bot.send_message.assert_not_called()
