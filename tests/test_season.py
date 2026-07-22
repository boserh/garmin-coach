"""NF-12 seasonal multisport intake: the setup-form parsing helper, `season` riding into
plan generation/extension/adaptation context (Claude mocked), and the `/plan/season`
edit-without-regeneration route."""
import datetime as dt
from unittest.mock import patch

from app.analysis import plans
from app.analysis.service import CallStats, run_plan_adaptation, run_plan_generation
from app.db.models import PlannedWorkout, TrainingPlan
from app.garmin.schemas import GeneratedPlan, PlanEdit, PlanWorkout
from app.routers.plan import _parse_season

U1 = 1


# --- _parse_season (pure) --------------------------------------------------------

def test_parse_season_valid_sport():
    assert _parse_season("kite", "4", "120") == {
        "sport": "kite", "sessions_per_week": 4, "avg_min": 120}


def test_parse_season_defaults_on_garbage_numbers():
    assert _parse_season("tennis", "abc", "") == {
        "sport": "tennis", "sessions_per_week": 3, "avg_min": 90}


def test_parse_season_empty_or_unknown_sport_returns_none():
    assert _parse_season("", "3", "90") is None
    assert _parse_season("golf", "3", "90") is None
    assert _parse_season(None, None, None) is None


# --- generation / extension context wiring ---------------------------------------

def _gen(summary="s"):
    return GeneratedPlan(summary=summary, workouts=[
        PlanWorkout(date="2026-07-01", week=1, type="easy", dist_km=4.0, description="легко"),
    ])


async def test_run_plan_generation_carries_season_into_context(session):
    seen: dict = {}

    def fake(context, api_key=None, model=None):
        seen.update(context)
        return _gen(), CallStats(kind="plan", model="m")

    with patch.object(plans, "generate_plan_with_stats", side_effect=fake):
        await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="x", target_date=None,
            start_date="2026-06-25", days_per_week=2, intensity="easy",
            intake={"season": {"sport": "kite", "sessions_per_week": 4, "avg_min": 120}},
            api_key=None,
        )
    assert seen["season"] == {"sport": "kite", "sessions_per_week": 4, "avg_min": 120}


async def test_run_plan_generation_season_absent_when_not_set(session):
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
    assert seen["season"] is None


# --- adaptation context wiring ----------------------------------------------------

async def _seed_plan(session, *, intake=None):
    fut = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-06-01", target_date=None, intake=intake)
    session.add(plan)
    await session.flush()
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date=fut, type="easy",
                               status="planned"))
    await session.commit()
    return plan


async def test_run_plan_adaptation_carries_season_from_plan_intake(session):
    await _seed_plan(session, intake={"season": {"sport": "tennis", "sessions_per_week": 2,
                                                  "avg_min": 60}})
    seen: dict = {}

    def fake(context, api_key=None):
        seen.update(context)
        return PlanEdit(summary="s", operations=[]), CallStats(kind="adapt", model="m")

    with patch.object(plans, "plan_adapt_with_stats", side_effect=fake):
        await run_plan_adaptation(session, user_id=U1)
    assert seen["season"] == {"sport": "tennis", "sessions_per_week": 2, "avg_min": 60}


async def test_run_plan_adaptation_season_none_without_intake(session):
    await _seed_plan(session, intake=None)
    seen: dict = {}

    def fake(context, api_key=None):
        seen.update(context)
        return PlanEdit(summary="s", operations=[]), CallStats(kind="adapt", model="m")

    with patch.object(plans, "plan_adapt_with_stats", side_effect=fake):
        await run_plan_adaptation(session, user_id=U1)
    assert seen["season"] is None
