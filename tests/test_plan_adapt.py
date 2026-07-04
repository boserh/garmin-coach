"""Adaptive plan (EP-02): correction proposals from compliance/recovery signals
(Claude mocked). Covers the window guardrail and the always-logged ReportLog."""
import datetime as dt
from unittest.mock import patch

from sqlalchemy import select

from app.analysis import service
from app.analysis.service import CallStats, run_plan_adaptation
from app.db.models import PlannedWorkout, ReportLog, TrainingPlan
from app.garmin.schemas import PlanEdit, PlanOp

U1 = 1


async def _seed_plan(session, *, workouts, status="active"):
    plan = TrainingPlan(
        user_id=U1, goal="g", status=status,
        start_date="2026-06-01", target_date="2026-09-01",
    )
    session.add(plan)
    await session.flush()
    for w in workouts:
        session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, **w))
    await session.commit()
    return plan


def _edit(ops, alt=None, risky=False, summary="s"):
    return PlanEdit(summary=summary, operations=ops, risky=risky, alt_operations=alt)


async def _adapt_logs(session):
    return (
        await session.execute(select(ReportLog).where(ReportLog.kind == "adapt"))
    ).scalars().all()


async def test_no_active_plan_returns_none(session):
    plan, edit = await run_plan_adaptation(session, user_id=U1)
    assert plan is None and edit is None
    assert await _adapt_logs(session) == []


async def test_empty_operations_is_a_noop_but_still_logged(session):
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    await _seed_plan(session, workouts=[dict(date=fut, type="easy", status="planned")])
    with patch.object(service, "plan_adapt_with_stats",
                       return_value=(_edit([]), CallStats(kind="adapt", model="m"))):
        plan, edit = await run_plan_adaptation(session, user_id=U1)
    assert plan is not None
    assert edit.operations == []
    logs = await _adapt_logs(session)
    assert len(logs) == 1 and logs[0].ok is True


async def test_ops_outside_window_are_dropped(session):
    near = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    far = (dt.date.today() + dt.timedelta(days=40)).isoformat()  # outside the 14-day window
    await _seed_plan(session, workouts=[dict(date=near, type="tempo", status="planned")])
    ops = [
        PlanOp(action="modify", date=near, dist_km=4.0),
        PlanOp(action="modify", date=far, dist_km=8.0),
    ]
    with patch.object(service, "plan_adapt_with_stats",
                       return_value=(_edit(ops), CallStats(kind="adapt", model="m"))):
        plan, edit = await run_plan_adaptation(session, user_id=U1, window_days=14)
    assert [op.date for op in edit.operations] == [near]


async def test_morning_trigger_keeps_only_today(session):
    today = dt.date.today().isoformat()
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    await _seed_plan(session, workouts=[dict(date=today, type="tempo", status="planned")])
    ops = [
        PlanOp(action="modify", date=today, dist_km=4.0),
        PlanOp(action="modify", date=tomorrow, dist_km=4.0),  # model overstepped
    ]
    with patch.object(service, "plan_adapt_with_stats",
                       return_value=(_edit(ops), CallStats(kind="adapt", model="m"))):
        plan, edit = await run_plan_adaptation(
            session, user_id=U1, trigger="morning", window_days=0,
        )
    assert [op.date for op in edit.operations] == [today]


async def test_alt_operations_also_filtered(session):
    near = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    far = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    await _seed_plan(session, workouts=[dict(date=near, type="long", status="planned")])
    ops = [PlanOp(action="modify", date=near, dist_km=6.0)]
    alt = [
        PlanOp(action="modify", date=near, dist_km=5.0),
        PlanOp(action="skip", date=far),
    ]
    with patch.object(service, "plan_adapt_with_stats",
                       return_value=(_edit(ops, alt=alt, risky=True),
                                     CallStats(kind="adapt", model="m"))):
        plan, edit = await run_plan_adaptation(session, user_id=U1, window_days=14)
    assert [op.date for op in edit.alt_operations] == [near]
