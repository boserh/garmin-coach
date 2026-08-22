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


# The default seed target is RELATIVE to today and far outside ADAPT_TAPER_DAYS: with a
# hardcoded date the whole file quietly changed behaviour once the wall clock came within
# two weeks of it (the taper's 15%-cut ceiling started dropping ops the conservative
# tests expect to survive). Tests that want the taper set their own near target.
_FAR_TARGET_DAYS = 60


def _far_target() -> str:
    return (dt.date.today() + dt.timedelta(days=_FAR_TARGET_DAYS)).isoformat()


async def _seed_plan(session, *, workouts, status="active", intake=None,
                     target_date=_far_target):
    plan = TrainingPlan(
        user_id=U1, goal="g", status=status,
        start_date="2026-06-01",
        target_date=target_date() if callable(target_date) else target_date,
        intake=intake,
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
    target = _far_target()
    await _seed_plan(session, target_date=target,
                     workouts=[dict(date=fut, type="easy", status="planned")])
    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1)
    assert seen["adjust_level"] == "conservative"
    assert seen["target_date"] == target
    assert seen["days_to_target"] == _FAR_TARGET_DAYS


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


# ---------- weekly compliance window (bug: future weeks read as "0 done") --------

async def test_compliance_excludes_weeks_that_have_not_happened_yet(session):
    """``weekly_compliance`` buckets every week the plan schedules, future ones
    included (needed for the /plan view). ``_recent_compliance`` used to take the
    lexically-last N weeks of that dict, which for a plan scheduled months ahead
    grabbed upcoming, not-yet-run weeks (always 0/N) instead of the real recent
    past — falsely reporting a compliance collapse. Seed one completed past week
    plus a run of future weeks and check only the past week survives."""
    today = dt.date.today()
    past = (today - dt.timedelta(days=10)).isoformat()
    future_dates = [(today + dt.timedelta(weeks=w)).isoformat() for w in range(2, 8)]
    workouts = [dict(date=past, type="easy", status="done")]
    workouts += [dict(date=d, type="easy", status="planned") for d in future_dates]
    await _seed_plan(session, workouts=workouts)

    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1)

    current_week = today.strftime("%G-W%V")
    assert seen["compliance"]
    assert all(week <= current_week for week in seen["compliance"])
    past_week = dt.date.fromisoformat(past).strftime("%G-W%V")
    assert seen["compliance"][past_week]["done"] == 1


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
    await _seed_plan(   # target_date set, far from the taper → conservative
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


# ---------- the CURRENT week's sessions are not misses yet -------------------

# A fixed Wednesday, so "what is still ahead this week" is the same fact on every run —
# with a relative reference date these tests would assert different things on a Sunday.
_WED = dt.date(2026, 8, 19)


async def test_current_week_sessions_still_ahead_are_not_counted_as_misses(session):
    """``done < total`` is what every prompt reads as "missed sessions". For a week that
    is over that holds; for the week in progress it does not — the back half is still
    planned. The morning and deload checks run daily, so a raw 2/4 handed the coach two
    phantom misses on a Wednesday and (per SYSTEM_PLAN_ADAPT) told it to stop adding
    volume. What is still ahead comes through as ``remaining`` instead."""
    monday = _WED - dt.timedelta(days=_WED.weekday())
    workouts = [
        dict(date=monday.isoformat(), type="easy", status="done"),
        dict(date=(monday + dt.timedelta(days=1)).isoformat(), type="easy", status="done"),
        dict(date=_WED.isoformat(), type="tempo", status="planned"),
        dict(date=(monday + dt.timedelta(days=5)).isoformat(), type="long", status="planned"),
    ]
    await _seed_plan(session, workouts=workouts)

    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1, today=_WED)

    week = seen["compliance"][_WED.strftime("%G-W%V")]
    assert week["done"] == 2
    assert week["total"] == 2          # only the sessions whose date has come
    assert week["remaining"] == 2      # Wednesday's own + Saturday's, still ahead


async def test_real_misses_in_the_current_week_still_read_as_misses(session):
    """The fix must not hide an actual gap: a past date this week that the matcher
    marked missed keeps counting against ``total``."""
    monday = _WED - dt.timedelta(days=_WED.weekday())
    await _seed_plan(session, workouts=[
        dict(date=monday.isoformat(), type="easy", status="missed"),
        dict(date=_WED.isoformat(), type="tempo", status="planned"),
    ])

    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1, today=_WED)

    week = seen["compliance"][_WED.strftime("%G-W%V")]
    assert week["done"] == 0 and week["total"] == 1 and week["remaining"] == 1


async def test_finished_weeks_carry_no_remaining_key(session):
    """A week that is over has nothing ahead — the field is dropped rather than sent as
    a zero, so the prompt only ever sees it when it means something."""
    past = _WED - dt.timedelta(days=10)
    await _seed_plan(session, workouts=[dict(date=past.isoformat(), type="easy", status="done")])

    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1, today=_WED)

    week = seen["compliance"][past.strftime("%G-W%V")]
    assert "remaining" not in week


async def test_plan_view_still_sees_the_whole_week(session):
    """``/plan`` renders "done/total" as progress through the week, so the raw bucket
    must keep counting every session — the due-so-far re-cut belongs to the LLM path."""
    from app.garmin import repository

    monday = _WED - dt.timedelta(days=_WED.weekday())
    plan = await _seed_plan(session, workouts=[
        dict(date=monday.isoformat(), type="easy", status="done"),
        dict(date=(monday + dt.timedelta(days=5)).isoformat(), type="long", status="planned"),
    ])
    raw = await repository.weekly_compliance(session, plan.id, _WED)
    week = raw[_WED.strftime("%G-W%V")]
    assert week["total"] == 2 and week["done"] == 1 and week["remaining"] == 1


# ---------- the readiness the model sees has an age -------------------------

async def test_stale_readiness_never_reaches_the_adaptation_context(session):
    """End to end: one bad day two and a half weeks back, nothing since (the watch went
    unworn). The coalesced snapshot used to hand the model "readiness 21, LOW, ACWR 145"
    as the current state, and the prompt turns that into a deload."""
    from app.db.models import DailyMetric

    session.add(DailyMetric(user_id=U1, date=(_WED - dt.timedelta(days=18)).isoformat(),
                            extra={"readiness_score": 21, "readiness_level": "LOW",
                                   "recovery_time_h": 72, "acwr_pct": 145, "vo2max": 46.5}))
    for back in range(0, 18):
        session.add(DailyMetric(user_id=U1, date=(_WED - dt.timedelta(days=back)).isoformat(),
                                extra={"resting_hr": 48}))
    await _seed_plan(session, workouts=[dict(date=_WED.isoformat(), type="tempo",
                                             status="planned")])

    seen: dict = {}
    with patch.object(plans, "plan_adapt_with_stats", side_effect=_capture(seen)):
        await run_plan_adaptation(session, user_id=U1, today=_WED)

    fitness = seen["fitness"]
    assert "readiness_score" not in fitness
    assert "acwr_pct" not in fitness
    assert fitness["vo2max"] == 46.5      # slow-moving metrics still coalesce over weeks
