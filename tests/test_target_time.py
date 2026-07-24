"""NF-17 · race target time — the intake field rides into plan-generation context and the
goal projection/digest get a real ``target_s`` (so a verdict can fire). Claude is mocked;
zero real calls."""
from unittest.mock import patch

from app.analysis import plans
from app.analysis.service import CallStats, run_plan_generation
from app.garmin.schemas import GeneratedPlan, PlanWorkout

U1 = 1


def _gen():
    return GeneratedPlan(summary="s", workouts=[
        PlanWorkout(date="2026-07-01", week=1, type="easy", dist_km=4.0, description="легко"),
    ])


async def test_run_plan_generation_carries_target_time_into_context(session):
    seen: dict = {}

    def fake(context, api_key=None, model=None):
        seen.update(context)
        return _gen(), CallStats(kind="plan", model="m")

    with patch.object(plans, "generate_plan_with_stats", side_effect=fake):
        await run_plan_generation(
            session, user_id=U1, goal="first_10k", goal_label="x", target_date="2026-10-01",
            start_date="2026-06-25", days_per_week=2, intensity="easy",
            intake={"target_time_s": 2940}, api_key=None,
        )
    assert seen["target_time_s"] == 2940


async def test_run_plan_generation_target_time_absent_when_not_set(session):
    seen: dict = {}

    def fake(context, api_key=None, model=None):
        seen.update(context)
        return _gen(), CallStats(kind="plan", model="m")

    with patch.object(plans, "generate_plan_with_stats", side_effect=fake):
        await run_plan_generation(
            session, user_id=U1, goal="first_10k", goal_label="x", target_date="2026-10-01",
            start_date="2026-06-25", days_per_week=2, intensity="easy",
            intake={}, api_key=None,
        )
    assert seen["target_time_s"] is None
