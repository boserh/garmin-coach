"""EP-10 phase 3: cycling-in-the-plan intake — the setup-form parsing helper, ``cycling``
riding into generation/extension context (Claude mocked). Mirrors test_season.py's shape."""
import datetime as dt
from unittest.mock import patch

from app.analysis import plans
from app.analysis.service import CallStats, run_plan_generation
from app.db.models import PlannedWorkout, TrainingPlan
from app.garmin.schemas import GeneratedPlan, PlanWorkout
from app.routers.plan import _parse_cycling

U1 = 1


# --- _parse_cycling (pure) --------------------------------------------------------

def test_parse_cycling_valid():
    assert _parse_cycling("on", ["tue", "sat"], "45") == {
        "days": ["tue", "sat"], "avg_min": 45}


def test_parse_cycling_normalizes_day_order():
    assert _parse_cycling("on", ["sat", "mon", "tue"], "60")["days"] == ["mon", "tue", "sat"]


def test_parse_cycling_dedupes_and_drops_unknown_days():
    assert _parse_cycling("on", ["tue", "tue", "bogus"], "60")["days"] == ["tue"]


def test_parse_cycling_defaults_avg_min_on_garbage():
    assert _parse_cycling("on", ["tue"], "abc") == {"days": ["tue"], "avg_min": 60}


def test_parse_cycling_unchecked_returns_none():
    assert _parse_cycling("", ["tue", "sat"], "45") is None


def test_parse_cycling_no_days_returns_none():
    assert _parse_cycling("on", [], "45") is None
    assert _parse_cycling("on", ["bogus"], "45") is None


# --- generation / extension context wiring ---------------------------------------

def _gen(summary="s"):
    return GeneratedPlan(summary=summary, workouts=[
        PlanWorkout(date="2026-07-01", week=1, type="easy", dist_km=4.0, description="легко"),
    ])


async def test_run_plan_generation_carries_cycling_into_context(session):
    seen: dict = {}

    def fake(context, api_key=None, model=None):
        seen.update(context)
        return _gen(), CallStats(kind="plan", model="m")

    with patch.object(plans, "generate_plan_with_stats", side_effect=fake):
        await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="x", target_date=None,
            start_date="2026-06-25", days_per_week=2, intensity="easy",
            intake={"cycling": {"days": ["tue", "sat"], "avg_min": 60}},
            api_key=None,
        )
    assert seen["cycling"] == {"days": ["tue", "sat"], "avg_min": 60}


async def test_run_plan_generation_cycling_absent_when_not_set(session):
    seen: dict = {}

    def fake(context, api_key=None, model=None):
        seen.update(context)
        return _gen(), CallStats(kind="plan", model="m")

    with patch.object(plans, "generate_plan_with_stats", side_effect=fake):
        await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="x", target_date=None,
            start_date="2026-06-25", days_per_week=2, intensity="easy", intake={},
            api_key=None,
        )
    assert seen["cycling"] is None


async def test_run_plan_generation_persists_cycling_type_workout(session):
    """A generated cycling-type workout round-trips through repository.create_plan/
    list_workouts unchanged (type is unconstrained str — no schema/DB rejection)."""
    gen = GeneratedPlan(summary="s", workouts=[
        PlanWorkout(date="2026-07-01", week=1, type="cycling", dist_km=25.0,
                    description="легка вело", steps=[
                        {"kind": "ride", "dist_m": 25000, "pace_min_km": None, "hr_zone": 2}]),
    ])
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(gen, CallStats(kind="plan", model="m"))):
        plan = await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="x", target_date=None,
            start_date="2026-06-25", days_per_week=2, intensity="easy",
            intake={"cycling": {"days": ["tue"], "avg_min": 60}}, api_key=None,
        )
    from app.garmin import repository

    ws = await repository.list_workouts(session, plan.id)
    assert len(ws) == 1 and ws[0].type == "cycling" and ws[0].dist_km == 25.0
    assert ws[0].steps[0]["kind"] == "ride"


async def test_run_plan_extension_carries_cycling_from_plan_intake(session):
    fut = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    plan = TrainingPlan(user_id=U1, goal="general", status="active",
                        start_date="2026-06-01", target_date=None,
                        intake={"cycling": {"days": ["thu"], "avg_min": 90}})
    session.add(plan)
    await session.flush()
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date=fut, type="easy",
                               status="done"))
    await session.commit()

    seen: dict = {}

    def fake(context, api_key=None, model=None):
        seen.update(context)
        return _gen(), CallStats(kind="plan", model="m")

    with patch.object(plans, "generate_plan_with_stats", side_effect=fake):
        await plans.run_plan_extension(session, user_id=U1, api_key=None)
    assert seen["cycling"] == {"days": ["thu"], "avg_min": 90}
