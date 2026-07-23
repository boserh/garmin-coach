"""EP-03: strength progression — week-by-week generated sessions (weight/reps growth +
deload), the deterministic guards around the Claude call, and the repository placement."""
import json
from unittest.mock import patch

from app.analysis import plans
from app.analysis.client import CallStats
from app.analysis.plans import (
    _fill_progression_gaps,
    _weeks_span,
    generate_strength_progression_with_stats,
)
from app.garmin import repository
from app.garmin.schemas import StrengthSession

U1 = 1


def _sess(name="Ноги"):
    return StrengthSession(name=name, warmup_s=300, blocks=[
        {"reps": 3, "rest_s": 90, "exercises": [
            {"category": "SQUAT", "exercise": None, "reps": 10, "weight_kg": 40.0}]}])


# ---------- _weeks_span ----------

def test_weeks_span_counts_inclusive_weeks():
    assert _weeks_span("2026-07-06", "2026-07-06") == 1
    assert _weeks_span("2026-07-06", "2026-07-12") == 1   # exactly one week
    assert _weeks_span("2026-07-06", "2026-07-13") == 2   # into the second week


def test_weeks_span_defaults_to_one_on_bad_input():
    assert _weeks_span(None, None) == 1
    assert _weeks_span("garbage", "2026-07-13") == 1
    assert _weeks_span("2026-07-13", "2026-07-06") == 1   # end before start


# ---------- _fill_progression_gaps ----------

def test_fill_progression_gaps_forward_and_back_fills():
    a, b, c = _sess("A"), _sess("B"), _sess("C")
    filled = _fill_progression_gaps([None, a, None, b, None, c, None])
    assert filled == [a, a, a, b, b, c, c]


def test_fill_progression_gaps_all_none_stays_all_none():
    assert _fill_progression_gaps([None, None]) == [None, None]


# ---------- generate_strength_progression_with_stats ----------

def _weeks_json(names):
    return json.dumps({"weeks": [
        {"name": n, "warmup_s": 300,
         "blocks": [{"reps": 3, "rest_s": 90,
                     "exercises": [{"category": "SQUAT", "exercise": None,
                                    "reps": 10, "weight_kg": 40.0}]}]}
        for n in names]})


def test_generate_strength_progression_parses_weeks_array():
    with patch.object(plans, "_complete",
                      return_value=(_weeks_json(["W1", "W2", "W3"]),
                                    CallStats(kind="plan", model="m"))):
        sessions, stats = generate_strength_progression_with_stats(
            {"description": "ноги", "weeks": 3})
    assert [s.name for s in sessions] == ["W1", "W2", "W3"]


def test_generate_strength_progression_pads_short_reply():
    """The model returns fewer weeks than asked — pad by repeating the last one rather
    than erroring (degrade gracefully)."""
    with patch.object(plans, "_complete",
                      return_value=(_weeks_json(["W1", "W2"]),
                                    CallStats(kind="plan", model="m"))):
        sessions, stats = generate_strength_progression_with_stats(
            {"description": "ноги", "weeks": 4})
    assert len(sessions) == 4
    assert [s.name for s in sessions] == ["W1", "W2", "W2", "W2"]


def test_generate_strength_progression_replicates_single_session_reply():
    """The model ignores the array format and returns one bare session (the old shape) —
    replicate it across every week rather than failing (pre-EP-03 behaviour, not an error)."""
    single = json.dumps({"name": "Одна", "warmup_s": 0, "blocks": [
        {"reps": 3, "rest_s": 60,
         "exercises": [{"category": "SQUAT", "exercise": None, "reps": 8, "weight_kg": None}]}]})
    with patch.object(plans, "_complete",
                      return_value=(single, CallStats(kind="plan", model="m"))):
        sessions, stats = generate_strength_progression_with_stats(
            {"description": "ноги", "weeks": 3})
    assert len(sessions) == 3
    assert all(s.name == "Одна" for s in sessions)


def test_generate_strength_progression_retries_then_raises():
    from app.analysis.client import AnalystError

    with patch.object(plans, "_complete",
                      return_value=("not json", CallStats(kind="plan", model="m"))), \
            __import__("pytest").raises(AnalystError):
        generate_strength_progression_with_stats({"description": "ноги", "weeks": 3})


# ---------- repository.add_strength_workouts: progression placement ----------

async def test_add_strength_workouts_progression_places_week_by_week(session):
    from app.db.models import TrainingPlan
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-06-30", target_date="2026-07-20")  # 3 tuesdays
    session.add(plan)
    await session.flush()
    sps = [repository._sanitize_strength(_sess(f"W{i}")) for i in (1, 2, 3)]

    n = await repository.add_strength_workouts(session, plan, {}, None, {"tue": sps})

    ws = sorted(await repository.list_workouts(session, plan.id), key=lambda w: w.date)
    assert n == 3 and len(ws) == 3
    assert [w.strength_plan["name"] for w in ws] == ["W1", "W2", "W3"]
    assert [w.week for w in ws] == [1, 2, 3]


async def test_add_strength_workouts_progression_clamps_to_last_entry(session):
    """More weekly occurrences than progression entries — the last entry repeats rather
    than erroring or running out."""
    from app.db.models import TrainingPlan
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-06-30", target_date="2026-07-20")  # 3 tuesdays
    session.add(plan)
    await session.flush()
    sps = [repository._sanitize_strength(_sess("Only"))]

    await repository.add_strength_workouts(session, plan, {}, None, {"tue": sps})

    ws = sorted(await repository.list_workouts(session, plan.id), key=lambda w: w.date)
    assert len(ws) == 3
    assert all(w.strength_plan["name"] == "Only" for w in ws)


async def test_add_strength_workouts_single_dict_unaffected_by_progression_support(session):
    """Backward compatibility: a plain dict (not a list) still lays the SAME session every
    week, exactly like before EP-03 — the confirmed-preview / extension-reuse path."""
    from app.db.models import TrainingPlan
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-06-30", target_date="2026-07-20")
    session.add(plan)
    await session.flush()
    sp = repository._sanitize_strength(_sess("Same"))

    await repository.add_strength_workouts(session, plan, {}, None, {"tue": sp})

    ws = await repository.list_workouts(session, plan.id)
    assert all(w.strength_plan["name"] == "Same" for w in ws)


# ---------- chat-edit isolation (AC: editing one week must not touch its siblings) ----------

async def test_modify_one_weeks_strength_leaves_siblings_untouched(session):
    from app.db.models import TrainingPlan
    from app.garmin.schemas import PlanOp

    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-06-30", target_date="2026-07-20")  # 3 tuesdays
    session.add(plan)
    await session.flush()
    sps = [repository._sanitize_strength(_sess(f"W{i}")) for i in (1, 2, 3)]
    await repository.add_strength_workouts(session, plan, {}, None, {"tue": sps})
    ws = sorted(await repository.list_workouts(session, plan.id), key=lambda w: w.date)
    week2_date = ws[1].date

    new_week2 = _sess("W2-edited")
    await repository.apply_plan_ops(session, plan, [
        PlanOp(action="modify", date=week2_date, strength=new_week2)])

    ws = sorted(await repository.list_workouts(session, plan.id), key=lambda w: w.date)
    assert [w.strength_plan["name"] for w in ws] == ["W1", "W2-edited", "W3"]
    assert [w.week for w in ws] == [1, 2, 3]   # week numbering untouched by the edit


# ---------- plan_sync naming: week suffix survives a named session (EP-03) ----------

async def test_push_workout_strength_progression_name_carries_week(session):
    from app.db.models import PlannedWorkout, TrainingPlan
    from app.garmin import plan_sync

    plan = TrainingPlan(user_id=U1, goal="g", status="active")
    session.add(plan)
    await session.flush()
    w = PlannedWorkout(
        plan_id=plan.id, user_id=U1, date="2026-07-07", week=2, type="strength",
        description="Ноги",
        strength_plan={"name": "Ноги", "warmup_s": 0, "blocks": []})
    session.add(w)
    await session.flush()

    with patch.object(plan_sync.client, "create_workout",
                      return_value={"workoutId": 1}) as create, \
            patch.object(plan_sync.client, "schedule_workout",
                         return_value={"workoutScheduleId": 2}):
        await plan_sync.push_workout(session, w)
    pushed_name = create.call_args[0][0]["workoutName"]
    assert pushed_name == "🏋️ Ноги · W2"
