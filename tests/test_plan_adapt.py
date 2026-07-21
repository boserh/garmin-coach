"""Adaptive plan (EP-02): correction proposals from compliance/recovery signals
(Claude mocked). Covers the window guardrail and the always-logged ReportLog."""
import datetime as dt
from unittest.mock import patch

from sqlalchemy import select

from app.analysis import plans
from app.analysis.service import CallStats, run_plan_adaptation
from app.db.models import ActivityRecord, PlannedWorkout, ReportLog, TrainingPlan
from app.garmin.schemas import PlanEdit, PlanOp

U1 = 1


async def _seed_plan(session, *, workouts, status="active", intake=None,
                     target_date="2026-09-01"):
    plan = TrainingPlan(
        user_id=U1, goal="g", status=status,
        start_date="2026-06-01", target_date=target_date, intake=intake,
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
    with patch.object(plans, "plan_adapt_with_stats",
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
    with patch.object(plans, "plan_adapt_with_stats",
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
    with patch.object(plans, "plan_adapt_with_stats",
                       return_value=(_edit(ops), CallStats(kind="adapt", model="m"))):
        plan, edit = await run_plan_adaptation(
            session, user_id=U1, trigger="morning", window_days=0,
        )
    assert [op.date for op in edit.operations] == [today]


# ---------- adjust level (ST-07) ----------

def _capture(seen):
    """A plan_adapt_with_stats stand-in that records the context it was given."""
    def fake(context, api_key=None):
        seen.update(context)
        return _edit([]), CallStats(kind="adapt", model="m")
    return fake


async def test_adjust_level_off_skips_the_claude_call(session):
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    await _seed_plan(session, workouts=[dict(date=fut, type="long", status="planned")],
                     intake={"adjust_level": "off"})
    with patch.object(plans, "plan_adapt_with_stats") as m:
        plan, edit = await run_plan_adaptation(session, user_id=U1)
    m.assert_not_called()
    assert plan is not None and edit is None
    assert await _adapt_logs(session) == []      # no call → no cost row


async def test_default_level_conservative_with_target_date(session):
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    await _seed_plan(session, workouts=[dict(date=fut, type="easy", status="planned")])
    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1)
    assert seen["adjust_level"] == "conservative"
    assert seen["target_date"] == "2026-09-01"
    assert seen["days_to_target"] == (dt.date(2026, 9, 1) - dt.date.today()).days


async def test_default_level_flexible_without_target_date(session):
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    await _seed_plan(session, workouts=[dict(date=fut, type="easy", status="planned")],
                     target_date=None)
    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1)
    assert seen["adjust_level"] == "flexible"
    assert seen["days_to_target"] is None


async def test_conservative_bounds_a_broken_morning_long(session):
    """The AC fixture: a wrecked morning with a long run planned. Conservative keeps
    only an eased (≤30% cut) or slightly moved long; cancelling it or shrinking it to
    a token 2 km is over the line and must be dropped by the guard."""
    today = dt.date.today()
    d0 = today.isoformat()
    plus1 = (today + dt.timedelta(days=1)).isoformat()
    plus5 = (today + dt.timedelta(days=5)).isoformat()
    target = (today + dt.timedelta(days=60)).isoformat()     # far from taper
    await _seed_plan(
        session, target_date=target,
        workouts=[dict(date=d0, type="long", dist_km=14.0, status="planned")])
    ops = [
        PlanOp(action="skip", date=d0),                       # cancel the long
        PlanOp(action="modify", date=d0, dist_km=2.0),        # token 2 km
        PlanOp(action="modify", date=d0, dist_km=10.0),       # −29% — allowed
        PlanOp(action="move", date=d0, to_date=plus1),        # 1 day — allowed
        PlanOp(action="move", date=d0, to_date=plus5),        # 5 days — too far
    ]
    with patch.object(plans, "plan_adapt_with_stats",
                       return_value=(_edit(ops), CallStats(kind="adapt", model="m"))):
        _plan, edit = await run_plan_adaptation(session, user_id=U1, window_days=14)
    assert [(op.action, op.dist_km or op.to_date) for op in edit.operations] == [
        ("modify", 10.0), ("move", plus1)]


async def test_flexible_allows_token_run_and_skip(session):
    today = dt.date.today().isoformat()
    await _seed_plan(
        session, target_date=None,   # health goal → flexible by default
        workouts=[dict(date=today, type="long", dist_km=14.0, status="planned")])
    ops = [
        PlanOp(action="modify", date=today, dist_km=2.0),
        PlanOp(action="skip", date=today),
    ]
    with patch.object(plans, "plan_adapt_with_stats",
                       return_value=(_edit(ops), CallStats(kind="adapt", model="m"))):
        _plan, edit = await run_plan_adaptation(session, user_id=U1, window_days=14)
    assert [op.action for op in edit.operations] == ["modify", "skip"]


# ---------- step-level context (NF-14) ----------

async def test_step_match_aggregate_enters_the_context(session):
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    plan = await _seed_plan(session, workouts=[dict(date=fut, type="easy", status="planned")])
    past = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    act = ActivityRecord(user_id=U1, activity_id=8888, date=past, type="running",
                         dist_km=8.0, dur_min=40.0,
                         step_match={"steps_hit": 3, "steps_total": 6, "misses": []})
    session.add(act)
    await session.flush()
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date=past, type="tempo",
                               status="done", completed_activity_id=act.id))
    await session.commit()

    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1)
    assert seen["step_match"] == {"sessions": 1, "steps_hit": 3, "steps_total": 6,
                                  "hit_rate": 0.5}


async def test_step_match_none_without_any_scored_sessions(session):
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    await _seed_plan(session, workouts=[dict(date=fut, type="easy", status="planned")])
    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1)
    assert seen["step_match"] is None


async def test_taper_allows_only_minimal_easing(session):
    today = dt.date.today()
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    target = (today + dt.timedelta(days=10)).isoformat()      # ≤14 days → taper
    await _seed_plan(
        session, target_date=target,
        workouts=[dict(date=tomorrow, type="tempo", dist_km=12.0, status="planned")])
    ops = [
        PlanOp(action="move", date=tomorrow,                  # no moves in the taper
               to_date=(today + dt.timedelta(days=2)).isoformat()),
        PlanOp(action="modify", date=tomorrow, dist_km=11.0),  # −8% — minimal, allowed
        PlanOp(action="modify", date=tomorrow, dist_km=8.0),   # −33% — too much
    ]
    with patch.object(plans, "plan_adapt_with_stats",
                       return_value=(_edit(ops), CallStats(kind="adapt", model="m"))):
        _plan, edit = await run_plan_adaptation(session, user_id=U1, window_days=14)
    assert [(op.action, op.dist_km) for op in edit.operations] == [("modify", 11.0)]


async def test_alt_operations_also_level_filtered(session):
    today = dt.date.today().isoformat()
    await _seed_plan(   # target_date default set → conservative
        session, workouts=[dict(date=today, type="long", dist_km=10.0, status="planned")])
    ops = [PlanOp(action="modify", date=today, dist_km=8.0)]
    alt = [PlanOp(action="skip", date=today)]                 # in-window but over the level
    with patch.object(plans, "plan_adapt_with_stats",
                       return_value=(_edit(ops, alt=alt, risky=True),
                                     CallStats(kind="adapt", model="m"))):
        _plan, edit = await run_plan_adaptation(session, user_id=U1, window_days=14)
    assert [op.action for op in edit.operations] == ["modify"]
    assert edit.alt_operations == []


async def test_alt_operations_also_filtered(session):
    near = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    far = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    await _seed_plan(session, workouts=[dict(date=near, type="long", status="planned")])
    ops = [PlanOp(action="modify", date=near, dist_km=6.0)]
    alt = [
        PlanOp(action="modify", date=near, dist_km=5.0),
        PlanOp(action="skip", date=far),
    ]
    with patch.object(plans, "plan_adapt_with_stats",
                       return_value=(_edit(ops, alt=alt, risky=True),
                                     CallStats(kind="adapt", model="m"))):
        plan, edit = await run_plan_adaptation(session, user_id=U1, window_days=14)
    assert [op.date for op in edit.alt_operations] == [near]
