"""Planned-vs-actual context in the /activity analysis: a matched PlannedWorkout
(matching.match_activities) should ride along in the payload so the analysis compares
fact to plan instead of narrating the run in isolation."""
import datetime as dt

from app.analysis import reports
from app.analysis.client import CallStats
from app.db.models import ActivityRecord, PlannedWorkout, TrainingPlan
from app.garmin import repository

U1 = 1
TODAY = dt.date.today().isoformat()


async def _mk_plan_and_workout(session, **kw):
    plan = TrainingPlan(
        user_id=U1, goal="first_5k", goal_label="Перші 5 км",
        start_date=TODAY, target_date=(dt.date.today() + dt.timedelta(weeks=8)).isoformat(),
        status="active",
    )
    session.add(plan)
    await session.flush()
    defaults = dict(
        plan_id=plan.id, user_id=U1, date=TODAY, week=1, type="easy",
        dist_km=5.0, description="Легкий біг, розмовний темп", status="planned",
    )
    w = PlannedWorkout(**{**defaults, **kw})
    session.add(w)
    await session.flush()
    return w


async def _mk_activity(session, **kw):
    defaults = dict(user_id=U1, activity_id=222, date=TODAY, type="running",
                     dist_km=5.0, dur_min=30.0)
    act = ActivityRecord(**{**defaults, **kw})
    session.add(act)
    await session.commit()
    await session.refresh(act)
    return act


def test_activity_payload_includes_planned_slice():
    w = PlannedWorkout(
        type="easy", dist_km=5.0, description="Легкий біг", status="done",
        steps=[{"kind": "run", "dist_m": 5000, "pace_min_km": [6.4, 6.6]}],
        match_info={"dist_delta_km": 0.1, "actual_pace_minkm": 6.0, "plan_pace_minkm": 6.5},
    )
    act = ActivityRecord(type="running", date=TODAY, dur_min=30.0, dist_km=5.0,
                          avg_hr=140, max_hr=155, load=80.0)

    data = reports.activity_payload(act, w)

    assert data["planned"] == {
        "type": "easy", "planned_dist_km": 5.0, "description": "Легкий біг",
        "plan_pace_minkm": "6:30", "actual_pace_minkm": "6:00", "dist_delta_km": 0.1,
        "status": "done",
    }


def test_an_interval_day_sends_no_whole_run_pace_comparison():
    """The 2026-09-30 report: the interval target (5:55–6:15) was compared with the average
    over warmup + jogs + cooldown (6:52), and the analyst spent a paragraph explaining why
    that comparison meant nothing. A target that covers only the work steps is no session
    target — the pace keys are left out, even for a row matched before this rule."""
    w = PlannedWorkout(
        type="intervals", dist_km=6.2, description="5×2 хв", status="done",
        steps=[{"kind": "warmup", "dist_m": 1500},
               {"kind": "repeat", "reps": 5, "steps": [
                   {"kind": "run", "dur_s": 120, "pace_min_km": [5.92, 6.25]},
                   {"kind": "recovery", "dur_s": 120}]},
               {"kind": "cooldown", "dist_m": 1500}],
        match_info={"dist_delta_km": 0.0, "actual_pace_minkm": 6.87, "plan_pace_minkm": 6.08},
    )
    act = ActivityRecord(type="running", date=TODAY, dur_min=42.4, dist_km=6.17)
    planned = reports.activity_payload(act, w)["planned"]
    assert "plan_pace_minkm" not in planned and "actual_pace_minkm" not in planned


def test_activity_payload_without_planned_omits_key():
    act = ActivityRecord(type="running", date=TODAY, dur_min=30.0, dist_km=5.0)
    assert "planned" not in reports.activity_payload(act)


async def test_get_workout_for_activity_scoped_to_user(session):
    w = await _mk_plan_and_workout(session)
    act = await _mk_activity(session)
    w.completed_activity_id = act.id
    await session.commit()

    got = await repository.get_workout_for_activity(session, U1, act.id)
    assert got is not None and got.id == w.id

    # A different user must not see this match.
    assert await repository.get_workout_for_activity(session, 999, act.id) is None


async def test_run_activity_analysis_feeds_planned_context(session, monkeypatch):
    """run_activity_analysis should look up the matched workout and pass it into the
    Claude payload — the fix for "no context of what was planned"."""
    w = await _mk_plan_and_workout(session, status="done")
    act = await _mk_activity(session)
    w.completed_activity_id = act.id
    w.match_info = {"dist_delta_km": 0.0, "actual_pace_minkm": 6.0, "plan_pace_minkm": 6.5}
    await session.commit()

    captured = {}

    def fake_analyze(activity_data, api_key=None):
        captured["data"] = activity_data
        return "аналіз", CallStats(kind="activity", model="m")

    monkeypatch.setattr(reports, "analyze_activity_with_stats", fake_analyze)

    text = await reports.run_activity_analysis(session, act, user_id=U1, api_key="k")

    assert text == "аналіз"
    assert captured["data"]["planned"]["status"] == "done"
    assert captured["data"]["planned"]["type"] == "easy"
    assert captured["data"]["planned"]["description"] == "Легкий біг, розмовний темп"


def test_narrated_paces_are_already_minutes_and_seconds():
    """2026-10-13: "7:30-7:70/км", "~7:78/км" — segment paces were the one place still
    handed over as decimal minutes. The /ask tools keep the decimals."""
    act = ActivityRecord(type="running", date=TODAY, dur_min=40.9, dist_km=6.01,
                         series=[{"d": 0.1 * i, "p": 7.78, "h": 130} for i in range(1, 6)])
    raw = reports.activity_payload(act)
    assert isinstance(raw["segments"][0]["avg_pace"], float)
    text = reports._paces_as_text(raw)
    assert text["avg_pace"] == "6:49"
    assert text["segments"][0]["avg_pace"] == "7:47"
    assert raw["segments"][0]["avg_pace"] == 7.78   # the copy, not the original


async def test_analysis_sees_the_earlier_structured_sessions(session, monkeypatch):
    sm = lambda d: {"steps_hit": 0, "steps_total": 1, "steps": [   # noqa: E731
        {"step": 2, "kind": "run", "planned": [6.08, 6.42], "actual": 5.77, "delta_s": d}]}
    day = dt.date.fromisoformat(TODAY)
    await _mk_activity(session, activity_id=331, step_match=sm(-25),
                       date=(day - dt.timedelta(days=7)).isoformat())
    await _mk_activity(session, activity_id=332, step_match=sm(-13),
                       date=(day - dt.timedelta(days=60)).isoformat())   # out of the window
    await _mk_activity(session, user_id=U1 + 1, activity_id=333, step_match=sm(-40),
                       date=(day - dt.timedelta(days=3)).isoformat())    # someone else's
    act = await _mk_activity(session, activity_id=334, step_match=sm(-18))

    captured = {}

    def fake_analyze(activity_data, api_key=None):
        captured["data"] = activity_data
        return "аналіз", CallStats(kind="activity", model="m")

    monkeypatch.setattr(reports, "analyze_activity_with_stats", fake_analyze)
    await reports.run_activity_analysis(session, act, user_id=U1, api_key="k")
    recent = captured["data"]["recent_structured"]
    assert [r["delta_s"] for r in recent] == [[-25]]
    assert recent[0]["date"] == (day - dt.timedelta(days=7)).isoformat()
