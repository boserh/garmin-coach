"""``recompute-step-match`` CLI command: re-score an already-scored NF-14 step_match after
a stepmatch/fetch_activity_splits bug fix — the ingest-time idempotency guard
(``bot.jobs._step_match_for_activity``) never touches a non-null step_match again, so a
wrong stored value stays wrong forever without this."""
from contextlib import asynccontextmanager

import pytest

from app import cli
from app.db.models import ActivityRecord, PlannedWorkout, TrainingPlan, User
from app.garmin import client


@pytest.fixture
def _cli_session(session, monkeypatch):
    """Route app.cli's async_session_maker/init_db/garmin_login to the test session,
    same shape as test_plansteps.py's _cli_session."""
    @asynccontextmanager
    async def maker():
        yield session

    async def _noop_init_db():
        return None

    @asynccontextmanager
    async def _noop_garmin_login(session, user):
        yield None

    monkeypatch.setattr(cli, "async_session_maker", maker)
    monkeypatch.setattr(cli, "init_db", _noop_init_db)
    monkeypatch.setattr(cli, "garmin_login", _noop_garmin_login)
    return session


_STEPS = [
    {"kind": "warmup", "dist_m": 1500},
    {"kind": "run", "dist_m": 2000, "pace_min_km": [6.17, 6.42]},
    {"kind": "cooldown", "dist_m": 2000},
]

# What the old, buggy positional pairing produced: the tempo step compared against the
# tail of the warmup lap.
_WRONG_STEP_MATCH = {"steps_hit": 0, "steps_total": 1,
                     "misses": [{"step": 2, "planned": [6.17, 6.42], "actual": 7.65}]}

# The real raw laps for that run (see the PR this backfill ships with): Auto Lap split
# each plan step into several ~1km physical laps, tagged with wkt_step_index.
_REAL_LAPS = [
    {"dist_m": 1000.0, "dur_s": 446.1, "wkt_step_index": 0},
    {"dist_m": 500.0, "dur_s": 229.5, "wkt_step_index": 0},
    {"dist_m": 1000.0, "dur_s": 368.4, "wkt_step_index": 1},
    {"dist_m": 1000.0, "dur_s": 346.8, "wkt_step_index": 1},
    {"dist_m": 1000.0, "dur_s": 450.2, "wkt_step_index": 2},
    {"dist_m": 1000.0, "dur_s": 438.9, "wkt_step_index": 2},
]


async def _seed(session, *, email="fix@x.com", activity_id=555):
    user = User(email=email, password_hash="h")
    session.add(user)
    await session.commit()
    plan = TrainingPlan(user_id=user.id, goal="g", status="active", start_date="2026-09-01")
    session.add(plan)
    await session.flush()
    act = ActivityRecord(user_id=user.id, activity_id=activity_id, date="2026-09-16",
                         type="running", dist_km=5.51, dur_min=38.1,
                         step_match=_WRONG_STEP_MATCH)
    session.add(act)
    await session.flush()
    workout = PlannedWorkout(
        plan_id=plan.id, user_id=user.id, date="2026-09-16", type="tempo",
        dist_km=5.5, description="tempo", steps=_STEPS, status="done",
        garmin_workout_id=99, completed_activity_id=act.id)
    session.add(workout)
    await session.commit()
    return user, act, workout


async def test_recompute_is_a_dry_run_by_default(_cli_session, monkeypatch, capsys):
    session = _cli_session
    _user, act, _w = await _seed(session)
    monkeypatch.setattr(client, "fetch_activity_splits", lambda *a, **kw: _REAL_LAPS)

    assert await cli._recompute_step_match("fix@x.com", apply=False, activity_id=None) == 0
    out = capsys.readouterr().out
    assert "0/1 -> 1/1" in out and "--apply" in out
    await session.refresh(act)
    assert act.step_match == _WRONG_STEP_MATCH   # untouched without --apply


async def test_recompute_applies_and_fixes_the_stored_value(_cli_session, monkeypatch):
    session = _cli_session
    _user, act, _w = await _seed(session)
    monkeypatch.setattr(client, "fetch_activity_splits", lambda *a, **kw: _REAL_LAPS)

    assert await cli._recompute_step_match("fix@x.com", apply=True, activity_id=None) == 0
    await session.refresh(act)
    assert act.step_match["steps_hit"] == 1
    assert act.step_match["misses"] == []


async def test_recompute_scopes_to_one_activity_row_id(_cli_session, monkeypatch, capsys):
    session = _cli_session
    _user, act, _w = await _seed(session, activity_id=777)
    monkeypatch.setattr(client, "fetch_activity_splits", lambda *a, **kw: _REAL_LAPS)

    assert await cli._recompute_step_match(
        "fix@x.com", apply=True, activity_id=act.id + 999) == 0
    out = capsys.readouterr().out
    assert "No matching" in out
    await session.refresh(act)
    assert act.step_match == _WRONG_STEP_MATCH   # the real row was never touched


async def test_recompute_skips_activities_with_no_structured_match(_cli_session, capsys):
    session = _cli_session
    user = User(email="none@x.com", password_hash="h")
    session.add(user)
    await session.commit()
    session.add(ActivityRecord(user_id=user.id, activity_id=1, date="2026-09-01",
                               type="running",
                               step_match={"steps_hit": 1, "steps_total": 1, "misses": []}))
    await session.commit()

    assert await cli._recompute_step_match("none@x.com", apply=True, activity_id=None) == 0
    out = capsys.readouterr().out
    assert "Nothing changed" in out


async def test_recompute_reports_nothing_without_any_scored_activity(_cli_session, capsys):
    session = _cli_session
    user = User(email="empty@x.com", password_hash="h")
    session.add(user)
    await session.commit()

    assert await cli._recompute_step_match("empty@x.com", apply=True, activity_id=None) == 0
    out = capsys.readouterr().out
    assert "No already-scored activities" in out
