"""Training-plan generation: JSON coercion + persistence (Claude mocked)."""
from unittest.mock import patch

from app.analysis import service
from app.analysis.service import CallStats, _coerce_plan, run_plan_generation
from app.garmin import repository
from app.garmin.schemas import GeneratedPlan, PlanWorkout

U1 = 1


def test_coerce_plan_handles_fenced_json():
    raw = ('```json\n{"summary": "підхід", "workouts": '
           '[{"date": "2026-07-01", "week": 1, "type": "easy", "dist_km": 4.0, '
           '"description": "легкий біг"}]}\n```')
    plan = _coerce_plan(raw)
    assert plan.summary == "підхід"
    assert plan.workouts[0].type == "easy" and plan.workouts[0].dist_km == 4.0


def test_coerce_plan_plain_and_empty_workouts():
    plan = _coerce_plan('{"summary": "x", "workouts": []}')
    assert plan.summary == "x" and plan.workouts == []


def _gen(summary="підхід", workouts=None):
    return GeneratedPlan(
        summary=summary,
        workouts=workouts if workouts is not None else [
            PlanWorkout(date="2026-07-01", week=1, type="easy", dist_km=4.0,
                        description="легко"),
            PlanWorkout(date="2026-07-03", week=1, type="rest", dist_km=None,
                        description="відпочинок"),
        ],
    )


async def test_run_plan_generation_persists_and_archives(session):
    with patch.object(service, "generate_plan_with_stats",
                      return_value=(_gen(), CallStats(kind="plan", model="m"))):
        plan = await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="Перші 5 км",
            target_date="2026-08-01", start_date="2026-06-25", days_per_week=3,
            intensity="moderate", intake={"notes": None}, api_key=None)
    assert plan.goal == "first_5k" and plan.status == "active"
    ws = await repository.list_workouts(session, plan.id)
    assert len(ws) == 2 and ws[0].type == "easy"

    # a second generation archives the first → only the newest stays active
    with patch.object(service, "generate_plan_with_stats",
                      return_value=(_gen(summary="новий", workouts=[]),
                                    CallStats(kind="plan", model="m"))):
        plan2 = await run_plan_generation(
            session, user_id=U1, goal="faster_5k", goal_label="Швидше 5 км",
            target_date=None, start_date="2026-06-25", days_per_week=3,
            intensity="easy", intake={}, api_key=None)
    active = await repository.get_active_plan(session, U1)
    assert active.id == plan2.id
