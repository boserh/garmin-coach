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
        match_info={"dist_delta_km": 0.1, "actual_pace_minkm": 6.0, "plan_pace_minkm": 6.5},
    )
    act = ActivityRecord(type="running", date=TODAY, dur_min=30.0, dist_km=5.0,
                          avg_hr=140, max_hr=155, load=80.0)

    data = reports.activity_payload(act, w)

    assert data["planned"] == {
        "type": "easy", "planned_dist_km": 5.0, "description": "Легкий біг",
        "plan_pace_minkm": 6.5, "actual_pace_minkm": 6.0, "dist_delta_km": 0.1,
        "status": "done",
    }


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
