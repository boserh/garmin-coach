"""The daily report must know HOW yesterday's planned session went, not only its averages.

The bug, from a morning report: yesterday was 5×600 m at 5:15–5:30 (reps actually run at
4:48–5:12, HR up to 164, RPE 7). ``recent_activities`` carried only the whole-run numbers —
6.8 km, 46 min, avg HR 136 — averaged over warm-up, rests and cool-down, and ``plan_today``
looks forward only. So the report called it «пробіжка у легкому темпі» and «RPE низький».
"""
import datetime as dt
from unittest.mock import patch

from app.analysis import reports
from app.analysis.cache import _cache_key
from app.analysis.client import CallStats
from app.db.models import ActivityRecord, PlannedWorkout, TrainingPlan

U1 = 1
_PAYLOAD = {"daily": [], "recent_activities": [], "planned_runs": []}

_STEP_MATCH = {
    "steps_hit": 1, "steps_total": 2,
    "misses": [{"step": 4, "planned": [5.25, 5.5], "actual": 4.8}],
    "steps": [
        {"step": 2, "kind": "run", "planned": [5.25, 5.5], "actual": 5.25, "hit": True,
         "delta_s": 0, "from_m": 1500, "to_m": 2100},
        {"step": 4, "kind": "run", "planned": [5.25, 5.5], "actual": 4.8, "hit": False,
         "delta_s": -27, "from_m": 2307, "to_m": 2907},
    ],
}


async def _seed(session, today: dt.date, *, hidden: bool = False):
    yday = (today - dt.timedelta(days=1)).isoformat()
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date=(today - dt.timedelta(days=30)).isoformat())
    session.add(plan)
    act = ActivityRecord(user_id=U1, activity_id=445, date=yday, type="running",
                         dist_km=6.8, dur_min=45.9, avg_hr=136, max_hr=164,
                         step_match=_STEP_MATCH, is_hidden=hidden,
                         subjective={"rpe": 7, "pain": True, "note": "гомілка"})
    session.add(act)
    await session.flush()
    session.add_all([
        PlannedWorkout(plan_id=plan.id, user_id=U1, date=yday, type="intervals",
                       dist_km=6.5, description="5×600 м у темпі 5:15–5:30/км",
                       status="done", completed_activity_id=act.id),
        # still planned → belongs to plan_today, never to plan_recent
        PlannedWorkout(plan_id=plan.id, user_id=U1, date=today.isoformat(),
                       type="strength", status="planned"),
    ])
    await session.commit()


async def _capture(session, today: dt.date):
    captured = {}

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                     api_key=None, weather=None, plan_today=None, fitness=None,
                     records=None, norm=None, subjective=None, health_alerts=None,
                     fueling=None, today=None, intensity_ctx=None,
                     athlete_profile=None, away_ctx=None, plan_recent=None):
        captured["plan_recent"] = plan_recent
        return "звіт", CallStats(kind=kind or "report", model="m")

    with patch.object(reports, "analyze_with_stats", fake_analyze):
        await reports.run_analysis(session, _PAYLOAD, user_id=U1, today=today)
    return captured["plan_recent"]


async def test_yesterdays_interval_session_reaches_the_report(session):
    today = dt.date.today()
    await _seed(session, today)

    rows = await _capture(session, today)

    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "intervals" and row["status"] == "done"
    actual = row["actual"]
    assert actual["max_hr"] == 164
    assert actual["subjective"] == {"rpe": 7, "pain": True, "note": "гомілка"}
    # paces arrive as "M:SS", and the step table is trimmed to what the report narrates
    assert actual["step_match"]["steps"][1] == {
        "step": 4, "kind": "run", "planned": "5:15-5:30", "actual": "4:48", "hit": False}


async def test_hidden_activity_is_not_narrated(session):
    today = dt.date.today()
    await _seed(session, today, hidden=True)

    rows = await _capture(session, today)

    assert rows == [{"date": (today - dt.timedelta(days=1)).isoformat(), "type": "intervals",
                     "dist_km": 6.5, "description": "5×600 м у темпі 5:15–5:30/км",
                     "status": "done"}]


async def test_no_resolved_session_means_no_field(session):
    assert await _capture(session, dt.date.today()) is None


def test_plan_recent_keys_the_cache_only_when_present():
    base = _cache_key({}, "q", "m")
    assert _cache_key({}, "q", "m", plan_recent=None) == base
    assert _cache_key({}, "q", "m", plan_recent=[{"date": "2026-10-21"}]) != base
