"""A ``repeat`` step must reach the watch with its count.

The bug: a chat edit to "5×600 м у темпі 5:15–5:30/км" stored the repeat block without
``reps`` (the plan-edit prompt never said a repeat needs one), ``workout_export`` read the
missing count as 1, and Garmin showed "1 time" — one 600 m rep. Asking the coach to fix it
produced the same shape again. Locked down at three layers: the prompt asks for ``reps``,
the schema accepts the count under the names a model reaches for, and the write path takes
it from the session's own description when it is still missing."""
import pytest

from app import plansteps
from app.analysis import prompts
from app.db.models import PlannedWorkout, TrainingPlan
from app.garmin import repository
from app.garmin.schemas import PlanOp, PlanStep
from app.garmin.workout_export import _build_steps

U1 = 1

DESCRIPTION = ("Розминка 1.5 км легко. Далі повтор ×5: 600 м у темпі 5:15–5:30/км + 120 с "
               "легкого підтюпцю між відрізками. Заминка 1.5 км легко.")

# What the model actually returned, both times: the repeat with no count.
COUNTLESS = [
    {"kind": "warmup", "dist_m": 1500.0},
    {"kind": "repeat", "steps": [
        {"kind": "run", "dist_m": 600.0, "pace_min_km": [5.25, 5.5]},
        {"kind": "recovery", "dur_s": 120, "note": "легкий підтюпцем"}]},
    {"kind": "cooldown", "dist_m": 1500.0},
]


def _reps(steps):
    return [s.get("reps") for s in steps if s.get("kind") == "repeat"]


# ---------- the prompt ----------

def test_the_edit_prompt_asks_for_reps_on_a_repeat():
    assert '"kind": "repeat", "reps":' in prompts.SYSTEM_PLAN_EDIT


# ---------- the schema ----------

@pytest.mark.parametrize("key", ["times", "count", "repeats", "iterations"])
def test_a_count_under_another_name_is_kept(key):
    step = PlanStep.model_validate({"kind": "repeat", key: 5,
                                    "steps": [{"kind": "run", "dist_m": 600}]})
    assert step.reps == 5


def test_an_explicit_reps_wins_over_an_alias():
    step = PlanStep.model_validate({"kind": "repeat", "reps": 4, "times": 6, "steps": []})
    assert step.reps == 4


def test_aliases_mean_nothing_outside_a_repeat():
    assert PlanStep.model_validate({"kind": "run", "count": 3, "dist_m": 100}).reps is None


# ---------- the description ----------

@pytest.mark.parametrize("text,n", [
    (DESCRIPTION, 5),
    ("Розминка 2 км, 6×400 м по 5:00, заминка", 6),
    ("4 x 3 хв у темпі 10 км", 4),
    ("8х200 м (кирилична х)", 8),
    ("5×600 м, повтор ×5", 5),            # the same count twice is still one count
])
def test_description_names_the_count(text, n):
    assert plansteps.description_reps(text) == n


@pytest.mark.parametrize("text", [
    None, "",
    "Легкий біг 6 км у зоні 2",
    "3×1 км і потім 4×200 м",              # two counts — no telling which is whose
    "×1 км у темпі",                        # a step length, not a count
    "13.10 - 2.5 км по 5:46",
])
def test_description_without_one_clear_count(text):
    assert plansteps.description_reps(text) is None


def test_fill_missing_reps_repairs_the_reported_session():
    steps, filled = plansteps.fill_missing_reps(COUNTLESS, DESCRIPTION)
    assert filled and _reps(steps) == [5]
    assert _reps(COUNTLESS) == [None]       # the input is not mutated


def test_fill_missing_reps_leaves_a_counted_repeat_alone():
    steps = [dict(COUNTLESS[1], reps=4)]
    assert plansteps.fill_missing_reps(steps, DESCRIPTION) == (steps, False)


def test_fill_missing_reps_declines_two_countless_repeats():
    steps = [COUNTLESS[1], COUNTLESS[1]]
    assert plansteps.fill_missing_reps(steps, DESCRIPTION) == (steps, False)


# ---------- the write path → the watch ----------

async def _seed(session):
    plan = TrainingPlan(user_id=U1, goal="g", status="active", start_date="2026-07-13")
    session.add(plan)
    await session.flush()
    session.add(PlannedWorkout(
        plan_id=plan.id, user_id=U1, date="2026-10-20", week=14, type="intervals",
        dist_km=6.5, description="старий опис", status="planned",
        steps=[{"kind": "warmup", "dist_m": 1500},
               {"kind": "repeat", "reps": 5, "steps": [
                   {"kind": "run", "dist_m": 600, "pace_min_km": [5.83, 6.17]},
                   {"kind": "recovery", "dur_s": 90}]},
               {"kind": "cooldown", "dist_m": 1500}]))
    await session.commit()
    return plan


async def test_a_countless_modify_reaches_the_watch_as_five_reps(session):
    plan = await _seed(session)
    op = PlanOp.model_validate({"action": "modify", "date": "2026-10-20", "dist_km": 6.5,
                                "description": DESCRIPTION, "steps": COUNTLESS})
    await repository.apply_plan_ops(session, plan, [op])
    w = (await repository.list_workouts(session, plan.id))[0]
    assert _reps(w.steps) == [5]
    group = [s for s in _build_steps(w.steps) if s["type"] == "RepeatGroupDTO"][0]
    assert group["numberOfIterations"] == 5


async def test_a_countless_add_takes_its_count_too(session):
    plan = await _seed(session)
    op = PlanOp.model_validate({"action": "add", "date": "2026-10-23", "type": "intervals",
                                "dist_km": 6.5, "description": DESCRIPTION,
                                "steps": COUNTLESS})
    await repository.apply_plan_ops(session, plan, [op])
    ws = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert _reps(ws["2026-10-23"].steps) == [5]
