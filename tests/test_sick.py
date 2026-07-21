"""Sick/travel mode (NF-03): block rebuild proposal, Claude mocked."""
import datetime as dt
from unittest.mock import patch

from sqlalchemy import select

from app.analysis import plans
from app.analysis.client import CallStats
from app.analysis.plans import _filter_sick_ops, run_sick_check
from app.db.models import ReportLog
from app.garmin import repository
from app.garmin.schemas import GeneratedPlan, PlanEdit, PlanOp, PlanWorkout

U1 = 1


def _iso(delta_days: int) -> str:
    return (dt.date.today() + dt.timedelta(days=delta_days)).isoformat()


async def _seed_plan(session, target_date=None):
    gen = GeneratedPlan(summary="план", workouts=[
        PlanWorkout(date=_iso(1), week=1, type="tempo", dist_km=8.0, description="темпова"),
        PlanWorkout(date=_iso(3), week=1, type="long", dist_km=18.0, description="довгий"),
        PlanWorkout(date=_iso(6), week=1, type="easy", dist_km=6.0, description="легко"),
    ])
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(gen, CallStats(kind="plan", model="m"))):
        return await plans.run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="x", target_date=target_date,
            start_date=_iso(-7), days_per_week=3, intensity="easy", intake={}, api_key=None)


def test_filter_sick_ops_keeps_only_allowed_actions_in_window():
    today = dt.date.today()
    ops = [
        PlanOp(action="skip", date=_iso(1)),
        PlanOp(action="modify", date=_iso(2), dist_km=3.0),
        PlanOp(action="add", date=_iso(2), type="easy", dist_km=5.0, description="x"),
        PlanOp(action="move", date=_iso(2), to_date=_iso(9999)),  # out of window via to_date
        PlanOp(action="skip", date=_iso(999)),                    # out of window via date
    ]
    kept = _filter_sick_ops(ops, today)
    assert [o.action for o in kept] == ["skip", "modify", "move"]


async def test_run_sick_check_no_active_plan_returns_none(session):
    plan, edit = await run_sick_check(session, user_id=U1, api_key=None)
    assert plan is None and edit is None


async def test_run_sick_check_proposes_without_applying(session):
    plan = await _seed_plan(session)
    edit = PlanEdit(summary="перебудовую блок, повертайся м'якше", operations=[
        PlanOp(action="skip", date=_iso(1)),
        PlanOp(action="modify", date=_iso(3), type="easy", dist_km=5.0, description="легко"),
    ])
    with patch.object(plans, "sick_with_stats",
                      return_value=(edit, CallStats(kind="sick", model="m"))):
        out_plan, out = await run_sick_check(
            session, user_id=U1, days_missed=3, api_key=None)
    assert out_plan.id == plan.id
    assert out.summary == "перебудовую блок, повертайся м'якше"
    assert [o.action for o in out.operations] == ["skip", "modify"]
    # proposed only — plan untouched
    ws = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert ws[_iso(1)].status == "planned"
    assert ws[_iso(3)].dist_km == 18.0


async def test_run_sick_check_filters_out_of_window_and_disallowed_ops(session):
    await _seed_plan(session)
    edit = PlanEdit(summary="x", operations=[
        PlanOp(action="skip", date=_iso(1)),
        PlanOp(action="add", date=_iso(2), type="easy", dist_km=5.0, description="new"),
        PlanOp(action="skip", date=_iso(999)),
    ])
    with patch.object(plans, "sick_with_stats",
                      return_value=(edit, CallStats(kind="sick", model="m"))):
        _plan, out = await run_sick_check(session, user_id=U1, api_key=None)
    assert [o.action for o in out.operations] == ["skip"]
    assert out.alt_operations is None


async def test_run_sick_check_logs_report(session):
    await _seed_plan(session)
    edit = PlanEdit(summary="x", operations=[])
    with patch.object(plans, "sick_with_stats",
                      return_value=(edit, CallStats(kind="sick", model="m"))):
        await run_sick_check(session, user_id=U1, api_key=None)
    rows = (await session.execute(
        select(ReportLog).where(ReportLog.user_id == U1, ReportLog.kind == "sick")
    )).scalars().all()
    assert len(rows) == 1 and rows[0].question == "sick:0"
