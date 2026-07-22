"""NF-14: the morning-tick hook that scores a matched activity's laps against its
planned structured steps, plus the DM badge."""
from types import SimpleNamespace
from unittest.mock import patch

from app.db.models import ActivityRecord, PlannedWorkout, TrainingPlan
from app.garmin import client
from bot import jobs as jobs_module

_STEPS = [{"kind": "run", "dist_m": 400, "pace_min_km": [4.5, 4.7]}]
_LAPS = [{"dist_m": 400.0, "dur_s": None, "pace_min_km": 4.6}]


async def _seed(session, *, garmin_workout_id=123, steps=_STEPS, step_match=None):
    plan = TrainingPlan(user_id=1, goal="g", status="active",
                        start_date="2026-06-01", target_date="2026-09-01")
    session.add(plan)
    await session.flush()
    act = ActivityRecord(user_id=1, activity_id=9999, date="2026-07-10",
                         type="running", dist_km=1.0, dur_min=5.0, step_match=step_match)
    session.add(act)
    await session.flush()
    w = PlannedWorkout(plan_id=plan.id, user_id=1, date="2026-07-10", week=1,
                       type="tempo", status="done", completed_activity_id=act.id,
                       garmin_workout_id=garmin_workout_id, steps=steps)
    session.add(w)
    await session.commit()
    await session.refresh(act)
    return act, w


async def test_step_match_computed_for_pushed_structured_session(session):
    act, _w = await _seed(session)
    with patch.object(client, "fetch_activity_splits", return_value=_LAPS) as m:
        await jobs_module._step_match_for_activity(session, 1, act)
    m.assert_called_once_with(9999)
    assert act.step_match == {"steps_hit": 1, "steps_total": 1, "misses": []}


async def test_step_match_skipped_when_not_pushed():
    act = SimpleNamespace(id=1, activity_id=9999, step_match=None)
    with patch.object(client, "fetch_activity_splits") as m:
        # no workout match at all → get_workout_for_activity returns None (no DB write)
        await jobs_module._step_match_for_activity(None, 1, act)
    m.assert_not_called()
    assert act.step_match is None


async def test_step_match_skipped_without_garmin_workout_id(session):
    act, _w = await _seed(session, garmin_workout_id=None)
    with patch.object(client, "fetch_activity_splits") as m:
        await jobs_module._step_match_for_activity(session, 1, act)
    m.assert_not_called()
    assert act.step_match is None


async def test_step_match_skipped_without_steps(session):
    act, _w = await _seed(session, steps=None)
    with patch.object(client, "fetch_activity_splits") as m:
        await jobs_module._step_match_for_activity(session, 1, act)
    m.assert_not_called()
    assert act.step_match is None


async def test_step_match_idempotent_when_already_scored(session):
    existing = {"steps_hit": 1, "steps_total": 1, "misses": []}
    act, _w = await _seed(session, step_match=existing)
    with patch.object(client, "fetch_activity_splits") as m:
        await jobs_module._step_match_for_activity(session, 1, act)
    m.assert_not_called()
    assert act.step_match == existing


async def test_step_match_failure_is_best_effort(session):
    act, _w = await _seed(session)
    with patch.object(client, "fetch_activity_splits", side_effect=RuntimeError("boom")):
        await jobs_module._step_match_for_activity(session, 1, act)   # must not raise
    assert act.step_match is None


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class _FakeCtx:
    def __init__(self):
        self.bot = _FakeBot()


async def test_activity_watch_dm_includes_step_badge(session, monkeypatch):
    import datetime as dt

    act, _w = await _seed(
        session, step_match={"steps_hit": 1, "steps_total": 1, "misses": []})
    act.date = dt.date.today().isoformat()   # inside ACTIVITY_FRESH_DAYS
    await session.commit()

    async def fake_analyze(session_, activity, *, user_id, api_key):
        return "аналіз"

    monkeypatch.setattr(jobs_module, "run_activity_analysis", fake_analyze)
    ctx = _FakeCtx()
    user = SimpleNamespace(id=1, telegram_chat_id=555)
    creds = SimpleNamespace(anthropic_key="k")

    await jobs_module._activity_watch_for_user(ctx, session, user, creds, [act])

    assert len(ctx.bot.sent) == 1
    assert "🎯 1/1 у цілі" in ctx.bot.sent[0][1]
