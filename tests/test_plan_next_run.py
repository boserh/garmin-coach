"""The morning report must always be able to answer «📅 наступна пробіжка».

The bug, from the screenshots: Thursday and Friday were both strength days, so the
report's own-plan window (today + tomorrow) held no run at all. The analyst fell through
to the Garmin-calendar fallback — which ``build_payload`` deliberately leaves empty once
we have our own plan — and told the athlete there was no next run, while ``/plan`` on the
same phone showed Saturday's easy 4.5 km.
"""
import datetime as dt
from unittest.mock import patch

from app.analysis import reports
from app.analysis.client import CallStats
from app.db.models import PlannedWorkout, TrainingPlan

U1 = 1
_PAYLOAD = {"daily": [], "recent_activities": [], "planned_runs": []}


async def _plan(session, today: dt.date, sessions):
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date=today.isoformat(),
                        target_date=(today + dt.timedelta(days=30)).isoformat())
    session.add(plan)
    await session.flush()
    for offset, type_, dist in sessions:
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=U1, type=type_, dist_km=dist, status="planned",
            date=(today + dt.timedelta(days=offset)).isoformat()))
    await session.commit()
    return plan


async def _capture_plan_today(session, today: dt.date):
    captured = {}

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                     api_key=None, weather=None, plan_today=None, fitness=None,
                     records=None, norm=None, subjective=None, health_alerts=None,
                     fueling=None, today=None, intensity_ctx=None,
                     athlete_profile=None, away_ctx=None):
        captured["plan_today"] = plan_today
        return "звіт", CallStats(kind=kind or "report", model="m")

    with patch.object(reports, "analyze_with_stats", fake_analyze):
        await reports.run_analysis(session, _PAYLOAD, user_id=U1, today=today)
    return captured["plan_today"]


async def test_next_run_beyond_the_window_reaches_the_report(session):
    today = dt.date.today()
    await _plan(session, today, [(0, "strength", None), (1, "strength", None),
                                 (2, "easy", 4.5)])

    plan_today = await _capture_plan_today(session, today)

    assert [s["date"] for s in plan_today] == [
        (today + dt.timedelta(days=i)).isoformat() for i in (0, 1, 2)]
    # the two-day window still decides what gets detailed advice; the extra entry is the
    # run only, and it carries its own distance so the report can name it
    assert plan_today[-1]["type"] == "easy" and plan_today[-1]["dist_km"] == 4.5


async def test_window_with_a_run_is_left_alone(session):
    """Tomorrow's run IS the next run — nothing further is dragged into the context."""
    today = dt.date.today()
    await _plan(session, today, [(0, "strength", None), (1, "easy", 4.0),
                                 (4, "long", 9.0)])

    plan_today = await _capture_plan_today(session, today)

    assert [s["type"] for s in plan_today] == ["strength", "easy"]


async def test_no_run_left_in_the_plan_adds_nothing(session):
    today = dt.date.today()
    await _plan(session, today, [(0, "strength", None), (1, "rest", None)])

    plan_today = await _capture_plan_today(session, today)

    assert [s["type"] for s in plan_today] == ["strength", "rest"]


async def test_fueling_still_only_looks_at_todays_session(session):
    """The appended run is days away — it must not become "today's session" for the
    heat/duration advisor (the ST-03 proximity rule)."""
    today = dt.date.today()
    await _plan(session, today, [(0, "strength", None), (3, "long", 12.0)])
    captured = {}

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                     api_key=None, weather=None, plan_today=None, fitness=None,
                     records=None, norm=None, subjective=None, health_alerts=None,
                     fueling=None, today=None, intensity_ctx=None,
                     athlete_profile=None, away_ctx=None):
        captured["fueling"] = fueling
        return "звіт", CallStats(kind=kind or "report", model="m")

    with patch.object(reports, "analyze_with_stats", fake_analyze):
        await reports.run_analysis(session, _PAYLOAD, user_id=U1, today=today,
                                   weather={"t_max": 30})

    assert captured["fueling"] is None


def test_prompt_forbids_leaking_internal_field_names():
    """The report literally shipped «(planned_runs порожній)» to the athlete."""
    from app.analysis import prompts

    assert "не згадуй у відповіді службові назви полів" in prompts.SYSTEM
