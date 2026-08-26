"""Training-plan generation: JSON coercion + persistence (Claude mocked)."""
from unittest.mock import patch

from app.analysis import plans
from app.analysis.service import (
    CallStats,
    _coerce_edit,
    _coerce_plan,
    run_plan_edit,
    run_plan_generation,
)
from app.garmin import repository
from app.garmin.schemas import GeneratedPlan, PlanEdit, PlanOp, PlanStep, PlanWorkout

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
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(_gen(), CallStats(kind="plan", model="m"))):
        plan = await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="Перші 5 км",
            target_date="2026-08-01", start_date="2026-06-25", days_per_week=3,
            intensity="moderate", intake={"notes": None}, api_key=None)
    assert plan.goal == "first_5k" and plan.status == "active"
    ws = await repository.list_workouts(session, plan.id)
    assert len(ws) == 2 and ws[0].type == "easy"

    # a second generation archives the first → only the newest stays active
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(_gen(summary="новий", workouts=[]),
                                    CallStats(kind="plan", model="m"))):
        plan2 = await run_plan_generation(
            session, user_id=U1, goal="faster_5k", goal_label="Швидше 5 км",
            target_date=None, start_date="2026-06-25", days_per_week=3,
            intensity="easy", intake={}, api_key=None)
    active = await repository.get_active_plan(session, U1)
    assert active.id == plan2.id


def test_coerce_plan_parses_structured_steps_with_repeat():
    raw = ('{"summary": "s", "workouts": [{"date": "2026-07-01", "week": 6, '
           '"type": "intervals", "dist_km": 6.0, "description": "d", "steps": ['
           '{"kind": "warmup", "dist_m": 1500, "pace_min_km": null}, '
           '{"kind": "repeat", "reps": 5, "steps": ['
           '{"kind": "run", "dur_s": 180, "pace_min_km": [5.25, 5.4]}, '
           '{"kind": "recovery", "dur_s": 120, "pace_min_km": null}]}, '
           '{"kind": "cooldown", "dist_m": 1500}]}]}')
    w = _coerce_plan(raw).workouts[0]
    assert w.steps[0].kind == "warmup" and w.steps[0].dist_m == 1500
    rep = w.steps[1]
    assert rep.kind == "repeat" and rep.reps == 5
    assert rep.steps[0].dur_s == 180 and rep.steps[0].pace_min_km == [5.25, 5.4]


async def test_run_plan_generation_persists_steps(session):
    gen = GeneratedPlan(summary="s", workouts=[PlanWorkout(
        date="2026-07-01", week=1, type="easy", dist_km=4.0, description="легко",
        steps=[PlanStep(kind="run", dist_m=4000, pace_min_km=[6.75, 7.0])])])
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(gen, CallStats(kind="plan", model="m"))):
        plan = await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="x", target_date=None,
            start_date="2026-06-25", days_per_week=2, intensity="easy", intake={}, api_key=None)
    ws = await repository.list_workouts(session, plan.id)
    # PlanStep persisted as a plain JSON dict, nulls dropped
    assert ws[0].steps == [{"kind": "run", "dist_m": 4000, "pace_min_km": [6.75, 7.0]}]


async def test_apply_plan_ops_add_carries_steps(session):
    plan = await _seed_plan(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-07-02", type="easy", dist_km=5.0, description="x",
        steps=[PlanStep(kind="run", dist_m=5000, pace_min_km=[6.75, 7.0])])])
    by_date = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert by_date["2026-07-02"].steps == [
        {"kind": "run", "dist_m": 5000, "pace_min_km": [6.75, 7.0]}]


def test_fmt_step_renders_human_labels():
    from app.routers.plan import _fmt_step, _pace
    assert _pace(6.75) == "6:45"
    assert _fmt_step({"kind": "run", "dist_m": 4000,
                      "pace_min_km": [6.75, 7.0]}) == "біг 4.0 км @ 6:45–7:00/км"
    assert _fmt_step({"kind": "warmup", "dist_m": 1500, "pace_min_km": None}) == "розминка 1.5 км"
    # sub-km steps show metres — 50 m and 100 m must be distinguishable (both were "0.1 км")
    assert _fmt_step({"kind": "run", "dist_m": 100,
                      "pace_min_km": [4.8, 5.1]}) == "біг 100 м @ 4:48–5:06/км"
    assert _fmt_step({"kind": "recovery", "dist_m": 50, "pace_min_km": None}) == "відновлення 50 м"
    rep = _fmt_step({"kind": "repeat", "reps": 5, "steps": [
        {"kind": "run", "dur_s": 180, "pace_min_km": [5.25, 5.4]},
        {"kind": "recovery", "dur_s": 120, "pace_min_km": None}]})
    assert rep == "5× (біг 3 хв @ 5:15–5:24/км + відновлення 2 хв)"


def test_est_minutes_from_steps():
    from app.routers.plan import _est_minutes
    # a single distance step: 3.5 km @ 7:00–7:24/км (mid 7.2) ≈ 25 min (explicit pace wins)
    assert _est_minutes([{"kind": "run", "dist_m": 3500,
                          "pace_min_km": [7.0, 7.4]}]) == 25
    # dur_s steps count verbatim; repeat multiplies; distance steps use their pace
    assert _est_minutes([
        {"kind": "warmup", "dist_m": 1500, "pace_min_km": [7.0, 7.2]},
        {"kind": "repeat", "reps": 5, "steps": [
            {"kind": "run", "dist_m": 400, "pace_min_km": [4.9, 5.1]},
            {"kind": "recovery", "dur_s": 60}]},
        {"kind": "cooldown", "dist_m": 1000, "pace_min_km": [7.0, 7.2]}]) == 33
    # a distance step with only an HR zone falls back to the default easy pace (6.5)
    assert _est_minutes([{"kind": "run", "dist_m": 5000, "hr_zone": 2}]) == 32
    # nothing to estimate → None (no '~хв' hint rendered)
    assert _est_minutes([]) is None
    assert _est_minutes(None) is None


def test_est_minutes_uses_anchor_and_zone():
    """HR-zone steps are timed off the user's typical (anchor) easy pace; a fast-zone
    stride is timed faster than the easy jog (the screenshot bug: everything at a flat 6.5)."""
    from app.routers.plan import _est_minutes
    # zone 2 (easy) @ anchor 7.1 → 5 km * 7.1 = 35.5 → 36 min (grounded, not the 6.5 guess)
    assert _est_minutes([{"kind": "run", "dist_m": 5000, "hr_zone": 2}], 7.1) == 36
    # a zone-5 stride is timed fast (7.1 * 0.76 ≈ 5.4/км), not at easy pace
    assert _est_minutes([{"kind": "run", "dist_m": 400, "hr_zone": 5}], 7.1) == 2
    # the screenshot workout (2.4 km easy + 4×(100 m + 100 m recovery), all zone 2) at a
    # real 7:06/km anchor → ~23 min, vs the misleading flat-6.5 guess (~21) without one.
    shot = [
        {"kind": "run", "dist_m": 2400, "hr_zone": 2},
        {"kind": "repeat", "reps": 4, "steps": [
            {"kind": "run", "dist_m": 100, "hr_zone": 2},
            {"kind": "recovery", "dist_m": 100}]}]
    assert _est_minutes(shot, 7.1) == 23
    assert _est_minutes(shot) == 21


def test_by_week_groups_by_calendar_monday():
    from types import SimpleNamespace

    from app.routers.plan import _by_week
    ws = [SimpleNamespace(date=d, week=1) for d in
          ("2026-07-02", "2026-07-05", "2026-07-07", "2026-07-12", "2026-07-14")]
    weeks = _by_week(ws)
    # 07-02(Thu)+07-05(Sun) share Mon 06-29; 07-07+07-12 share Mon 07-06; 07-14 → Mon 07-13
    assert [[w.date for w in items] for _, _, _, items, _ in weeks] == [
        ["2026-07-02", "2026-07-05"],
        ["2026-07-07", "2026-07-12"],
        ["2026-07-14"],
    ]
    assert weeks[0][0] == 1 and "чер" in weeks[0][1] and "лип" in weeks[0][1]
    assert all(collapsed is False for *_, collapsed in weeks)   # no `today` → nothing collapses


def _week_rows(*dates, status="planned"):
    from types import SimpleNamespace
    return [SimpleNamespace(date=d, week=1, status=status) for d in dates]


def test_by_week_collapses_only_fully_past_weeks():
    """ST-22: a week whose Sunday is already gone folds away; the current week never
    splits in half, so it stays open even though part of it is behind us."""
    from app.routers.plan import _by_week

    ws = _week_rows("2026-07-02", "2026-07-09", "2026-07-14", "2026-07-16")
    # 2026-07-14 is a Tuesday → the 07-13..07-19 week is current, 06-29 and 07-06 are past.
    weeks = _by_week(ws, "2026-07-14")
    assert [collapsed for *_, collapsed in weeks] == [True, True, False]


def test_by_week_keeps_the_last_completed_week_open():
    """The one bit of the past worth seeing daily: where you're coming from."""
    from app.routers.plan import _by_week

    ws = _week_rows("2026-07-02") + _week_rows("2026-07-09", status="done") + \
        _week_rows("2026-07-16")
    weeks = _by_week(ws, "2026-07-16")
    assert [collapsed for *_, collapsed in weeks] == [True, False, False]


def test_by_week_archived_plan_keeps_only_the_last_week_open():
    """Everything is past on an archived plan — collapsing all of it would open the
    page as an empty accordion."""
    from app.routers.plan import _by_week

    ws = _week_rows("2026-07-02", "2026-07-09", "2026-07-16", status="done")
    weeks = _by_week(ws, "2026-07-30", readonly=True)
    assert [collapsed for *_, collapsed in weeks] == [True, True, False]


def test_coerce_edit_parses():
    e = _coerce_edit(
        '{"summary": "додаю", "operations": [{"action": "add", "date": "2026-07-02", '
        '"type": "easy", "dist_km": 5.0, "description": "легко"}]}'
    )
    assert e.summary == "додаю" and e.operations[0].action == "add"
    # a plain edit defaults to not-risky with no alternative
    assert e.risky is False and e.alt_operations is None


def test_coerce_edit_parses_risky_with_alternative():
    raw = ('{"summary": "20 км швидко — різкий стрибок, ризик травми", "risky": true, '
           '"operations": [{"action": "modify", "date": "2026-08-22", "dist_km": 20.0, '
           '"type": "tempo"}], '
           '"alt_summary": "Краще 8 км легко", '
           '"alt_operations": [{"action": "modify", "date": "2026-08-22", "dist_km": 8.0, '
           '"type": "easy"}]}')
    e = _coerce_edit(raw)
    assert e.risky is True
    assert e.operations[0].dist_km == 20.0          # the literal request is preserved
    assert e.alt_operations[0].dist_km == 8.0       # the safer counter-proposal


def test_ops_hint_label():
    from bot.handlers import _ops_hint
    assert _ops_hint([{"action": "modify", "date": "x", "dist_km": 20.0}]) == " · 20 км"
    assert _ops_hint([{"action": "skip", "date": "x"}]) == ""
    # a swap shows the new exercise (label falls back to prettified code w/o translations)
    hint = _ops_hint([{"action": "swap_exercise", "date": "x", "to_category": "DEADLIFT"}])
    assert hint == " · Deadlift"
    # a from-scratch strength add shows its name
    assert _ops_hint([{"action": "add", "date": "x", "type": "strength",
                       "strength": {"name": "Ноги"}}]) == " · 🏋️ Ноги"


async def test_apply_plan_ops_swap_exercise(session):
    plan = await _seed_plan(session)
    # a strength day to edit
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-07-02", type="strength",
        garmin_template_id=931013083, description="Day 1")])
    # valid swap → appended to exercise_edits (codes upper-cased, variant + reps carried)
    affected = await repository.apply_plan_ops(session, plan, [PlanOp(
        action="swap_exercise", date="2026-07-02", from_category="hyperextension",
        to_category="deadlift", exercise="romanian_deadlift", reps=10)])
    assert len(affected) == 1
    w = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-02"]
    assert w.exercise_edits == [{"from": "HYPEREXTENSION", "to": "DEADLIFT",
                                 "exercise": "ROMANIAN_DEADLIFT", "reps": 10}]
    # an unmapped/invalid target category is rejected (nothing appended)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="swap_exercise", date="2026-07-02", from_category="PLANK",
        to_category="NOT_A_REAL_CATEGORY")])
    w2 = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-02"]
    assert len(w2.exercise_edits) == 1  # unchanged
    # a valid category but a hallucinated exercise name → swap still applies, but the name
    # is dropped to None (a bare category is valid on the watch); category is kept
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="swap_exercise", date="2026-07-02", from_category="CURL",
        to_category="SQUAT", exercise="NOT_A_REAL_EXERCISE")])
    w3 = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-02"]
    assert w3.exercise_edits[-1] == {"from": "CURL", "to": "SQUAT",
                                     "exercise": None, "reps": None}


async def test_apply_plan_ops_add_strength_from_scratch(session):
    plan = await _seed_plan(session)
    affected = await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-07-02", type="strength", description="Ноги",
        strength={"name": "Ноги", "warmup_s": 300, "blocks": [
            {"reps": 3, "rest_s": 90, "exercises": [
                {"category": "squat", "exercise": "goblet_squat", "reps": 12, "weight_kg": 20},
                {"category": "NOT_A_CAT", "reps": 10}]},   # invalid category dropped
            {"reps": 3, "exercises": []},                  # empty block dropped
        ]})])
    assert len(affected) == 1
    w = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-02"]
    sp = w.strength_plan
    assert sp["name"] == "Ноги" and sp["warmup_s"] == 300
    assert len(sp["blocks"]) == 1                          # only the valid block survives
    exs = sp["blocks"][0]["exercises"]
    assert len(exs) == 1 and exs[0]["category"] == "SQUAT"  # codes upper-cased
    assert exs[0]["exercise"] == "GOBLET_SQUAT" and exs[0]["weight_kg"] == 20
    # nothing valid → strength_plan stays None (won't push a broken session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-07-05", type="strength",
        strength={"blocks": [{"reps": 3, "exercises": [{"category": "BOGUS"}]}]})])
    w2 = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-05"]
    assert w2.strength_plan is None
    # a valid category with a hallucinated exercise name → exercise nulled, category kept
    # (the step stays a valid bare-category step, not dropped)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-07-06", type="strength",
        strength={"blocks": [{"reps": 3, "exercises": [
            {"category": "squat", "exercise": "totally_made_up", "reps": 10}]}]})])
    w3 = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-06"]
    ex = w3.strength_plan["blocks"][0]["exercises"][0]
    assert ex["category"] == "SQUAT" and ex["exercise"] is None


def test_check_exercise():
    from app.garmin import exercises
    # valid variant → normalised to the upper code
    assert exercises.check_exercise("squat", "goblet_squat") == "GOBLET_SQUAT"
    # empty/absent name → None (a bare category is valid)
    assert exercises.check_exercise("SQUAT", None) is None
    assert exercises.check_exercise("SQUAT", "") is None
    # hallucinated name under a real category → None (category-only step survives upstream)
    assert exercises.check_exercise("SQUAT", "NOT_A_REAL_EXERCISE") is None
    # catalog absent → can't validate the variant, so accept it (graceful degradation)
    with patch.object(exercises, "CATALOG", {}):
        assert exercises.check_exercise("SQUAT", "anything_goes") == "ANYTHING_GOES"


async def _seed_plan(session):
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(_gen(), CallStats(kind="plan", model="m"))):
        return await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="x", target_date=None,
            start_date="2026-06-25", days_per_week=3, intensity="easy", intake={}, api_key=None)


async def test_apply_plan_ops(session):
    plan = await _seed_plan(session)  # workouts on 2026-07-01 (easy) + 2026-07-03 (rest)
    affected = await repository.apply_plan_ops(session, plan, [
        PlanOp(action="add", date="2026-07-02", type="easy", dist_km=5.0, description="новий"),
        PlanOp(action="modify", date="2026-07-01", dist_km=6.0),
        PlanOp(action="move", date="2026-07-03", to_date="2026-07-04"),
    ])
    assert len(affected) == 3
    by_date = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert by_date["2026-07-02"].description == "новий"
    assert by_date["2026-07-01"].dist_km == 6.0
    assert "2026-07-04" in by_date and "2026-07-03" not in by_date

    await repository.apply_plan_ops(session, plan, [PlanOp(action="skip", date="2026-07-01")])
    by_date = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert by_date["2026-07-01"].status == "skipped"


async def test_apply_plan_ops_add_strength_carries_template(session):
    plan = await _seed_plan(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-07-02", type="strength",
        garmin_template_id=931013083, description="Day 1")])
    w = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-02"]
    assert w.type == "strength" and w.garmin_template_id == 931013083 and w.description == "Day 1"


async def test_add_strength_workouts_fixed_weekday_pairing(session):
    from app.db.models import TrainingPlan
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-07-06", target_date="2026-07-19")
    session.add(plan)
    await session.flush()
    # Fixed pairing: Mon → Day 1, Thu → Day 2 (same every week, not a rotation).
    n = await repository.add_strength_workouts(session, plan, {
        "mon": {"id": 931013083, "name": "Day 1"},
        "thu": {"id": 937200561, "name": "Day 2"},
    })
    ws = await repository.list_workouts(session, plan.id)   # ordered by date
    # Mon 07-06, Thu 07-09, Mon 07-13, Thu 07-16 → 4 sessions, each weekday keeps its workout
    assert n == 4 and len(ws) == 4
    assert all(w.type == "strength" for w in ws)
    assert [w.garmin_template_id for w in ws] == [931013083, 937200561, 931013083, 937200561]
    assert [w.description for w in ws] == ["Day 1", "Day 2", "Day 1", "Day 2"]


async def test_add_strength_workouts_stores_snapshot(session):
    from app.db.models import TrainingPlan
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-07-06", target_date="2026-07-12")
    session.add(plan)
    await session.flush()
    snaps = {931013083: {"name": "Day 1",
                         "exercises": [{"category": "SQUAT", "exercise": None, "reps": 10}]}}
    await repository.add_strength_workouts(
        session, plan, {"mon": {"id": 931013083, "name": "Day 1"}}, snaps)
    w = (await repository.list_workouts(session, plan.id))[0]
    # The template's exercises are snapshotted onto the row so /plan renders from the DB.
    assert w.strength_snapshot == snaps[931013083]


async def test_add_strength_workouts_custom_lays_strength_plan(session):
    from app.db.models import TrainingPlan
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-07-06", target_date="2026-07-19")  # two weeks
    session.add(plan)
    await session.flush()
    sp = {"name": "Ноги", "warmup_s": 300,
          "blocks": [{"reps": 3, "rest_s": 90,
                      "exercises": [{"category": "SQUAT", "exercise": None,
                                     "reps": 10, "weight_kg": 40.0}]}]}
    n = await repository.add_strength_workouts(
        session, plan, {}, None, {"wed": sp})
    ws = await repository.list_workouts(session, plan.id)
    assert n == 2                       # every Wednesday in the range
    assert all(w.type == "strength" for w in ws)
    assert all(w.garmin_template_id is None for w in ws)   # from-scratch, not a clone
    assert ws[0].strength_plan == sp
    assert ws[0].description == "Ноги"


def test_resolve_plan_model_maps_toggle():
    from app.analysis import service as svc
    assert svc.resolve_plan_model("opus") == svc.MODEL_PLAN_GEN
    assert svc.resolve_plan_model("fable") == svc.MODEL_PLAN_GEN_ALT
    assert svc.resolve_plan_model("nonsense") == svc.MODEL_PLAN_GEN   # safe default
    assert svc.resolve_plan_model(None) == svc.MODEL_PLAN_GEN


async def test_generate_strength_add_then_swaps_same_call(session):
    """Generation flow: add a strength day from a template + swap its exercises toward a
    focus, all in one apply_plan_ops call (the swap must find the just-added workout)."""
    plan = await _seed_plan(session)
    affected = await repository.apply_plan_ops(session, plan, [
        PlanOp(action="add", date="2026-07-02", type="strength",
               garmin_template_id=931013083, description="Ноги (як Day 1)"),
        PlanOp(action="swap_exercise", date="2026-07-02",
               from_category="BENCH_PRESS", to_category="SQUAT"),
        PlanOp(action="swap_exercise", date="2026-07-02",
               from_category="ROW", to_category="LEG_CURL"),
    ])
    assert len(affected) == 3
    w = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-02"]
    assert w.type == "strength" and w.garmin_template_id == 931013083
    assert w.exercise_edits == [
        {"from": "BENCH_PRESS", "to": "SQUAT", "exercise": None, "reps": None},
        {"from": "ROW", "to": "LEG_CURL", "exercise": None, "reps": None},
    ]


def test_read_exercises_parses_template():
    from app.garmin.workout_export import read_exercises
    raw = {"workoutSegments": [{"workoutSteps": [
        {"category": "BENCH_PRESS", "exerciseName": "BARBELL_BENCH_PRESS",
         "endCondition": {"conditionTypeKey": "reps"}, "endConditionValue": 8.0},
        {"category": "PLANK", "exerciseName": None,
         "endCondition": {"conditionTypeKey": "time"}, "endConditionValue": 60.0},
        {"workoutSteps": [{"category": "ROW", "exerciseName": "DUMBBELL_ROW",
                           "endCondition": {"conditionTypeKey": "reps"},
                           "endConditionValue": 10.0}]},
    ]}]}
    assert read_exercises(raw) == [
        {"category": "BENCH_PRESS", "exercise": "BARBELL_BENCH_PRESS", "reps": 8},
        {"category": "PLANK", "exercise": None, "reps": None},  # time-based → no reps
        {"category": "ROW", "exercise": "DUMBBELL_ROW", "reps": 10},  # nested repeat group
    ]


async def test_run_plan_edit_proposes_without_applying(session):
    plan = await _seed_plan(session)
    edit = PlanEdit(summary="додаю біг", operations=[
        PlanOp(action="add", date="2026-07-02", type="easy", dist_km=5.0, description="легко")])
    with patch.object(plans, "plan_edit_with_stats",
                      return_value=(edit, CallStats(kind="plan_edit", model="m"))):
        _plan, out = await run_plan_edit(
            session, user_id=U1, instruction="додай біг 2 липня", api_key=None)
    assert out.summary == "додаю біг"
    # proposed only — not yet written
    assert all(w.date != "2026-07-02" for w in await repository.list_workouts(session, plan.id))


async def test_run_plan_edit_feeds_template_blocks_to_model(session):
    """A "схоже на Day 1" edit gives the model the template's real block structure
    (sets/rest/weight), not just a flat exercise list — so it can mirror the loading."""
    from app.garmin import client

    plan = await _seed_plan(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-07-02", type="strength",
        garmin_template_id=931013083, description="Day 1")])
    raw = {"workoutSegments": [{"workoutSteps": [
        {"type": "RepeatGroupDTO", "stepType": {"stepTypeKey": "repeat"},
         "numberOfIterations": 3, "workoutSteps": [
            {"stepType": {"stepTypeKey": "interval"}, "category": "SQUAT",
             "endCondition": {"conditionTypeKey": "reps"}, "endConditionValue": 12.0,
             "weightValue": 20.0},
            {"stepType": {"stepTypeKey": "rest"}, "endConditionValue": 90.0}]},
    ]}]}
    captured = {}

    def fake_edit(context, api_key=None):
        captured["context"] = context
        return PlanEdit(summary="ok", operations=[]), CallStats(kind="plan_edit", model="m")

    with patch.object(client, "fetch_workout_full", return_value=raw), \
         patch.object(plans, "plan_edit_with_stats", side_effect=fake_edit):
        await run_plan_edit(session, user_id=U1,
                            instruction="додай силову як Day 1 але на ноги", api_key=None)

    tmpl = captured["context"]["strength_templates"][0]
    assert tmpl["blocks"] == [{"reps": 3, "rest_s": 90, "exercises": [
        {"category": "SQUAT", "exercise": None, "reps": 12, "weight_kg": 20.0}]}]


# --- "Відпочинок" that contradicts a real session on the same date -----------------
# The generator writes a "силовий/відпочинок за планом, бігу немає" row for a Monday it
# knows is a strength day; add_strength_workouts then puts the strength session on that
# same date, and /plan stacks "Відпочинок" on top of "🏋️ Силова". A day with no session
# already means rest, so the note is only ever redundant once something real lands there.

async def _plan_with_rest(session, *, start="2026-07-06", end="2026-07-12"):
    from app.db.models import TrainingPlan
    plan = TrainingPlan(user_id=U1, goal="general", status="active",
                        start_date=start, target_date=end)
    session.add(plan)
    await session.flush()
    return plan


async def test_strength_day_drops_the_generated_rest_note(session):
    plan = await _plan_with_rest(session)
    await repository.append_workouts(session, plan, [PlanWorkout(
        date="2026-07-06", week=1, type="rest", dist_km=None,
        description="Силовий/відпочинок за планом. Бігу немає.")])
    await repository.add_strength_workouts(
        session, plan, {"mon": {"id": 931013083, "name": "Day 1"}})
    ws = await repository.list_workouts(session, plan.id)
    assert [w.type for w in ws] == ["strength"]     # the rest note is gone, not stacked


async def test_generated_rest_row_yields_to_a_run_on_the_same_date(session):
    plan = await _plan_with_rest(session)
    await repository.append_workouts(session, plan, [
        PlanWorkout(date="2026-07-07", week=1, type="rest", dist_km=None,
                    description="бігу немає"),
        PlanWorkout(date="2026-07-07", week=1, type="easy", dist_km=4.0,
                    description="легкий біг"),
    ])
    ws = await repository.list_workouts(session, plan.id)
    assert [w.type for w in ws] == ["easy"]


async def test_lone_rest_day_survives(session):
    """A rest row alone on its date is the only thing carrying the reason — keep it."""
    plan = await _plan_with_rest(session)
    await repository.append_workouts(session, plan, [PlanWorkout(
        date="2026-07-08", week=1, type="rest", dist_km=None,
        description="повний відпочинок після хайкінгу")])
    await repository.add_strength_workouts(
        session, plan, {"mon": {"id": 931013083, "name": "Day 1"}})   # a different weekday
    ws = await repository.list_workouts(session, plan.id)
    assert {w.type for w in ws} == {"rest", "strength"}
    assert [w.date for w in ws if w.type == "rest"] == ["2026-07-08"]


async def test_prune_keeps_a_rest_day_already_acted_on(session):
    """Only untouched rows go: a rest day the athlete/an adaptation already resolved is
    history, not a placeholder."""
    plan = await _plan_with_rest(session)
    await repository.append_workouts(session, plan, [PlanWorkout(
        date="2026-07-06", week=1, type="rest", dist_km=None, description="відпочинок")])
    rest = (await repository.list_workouts(session, plan.id))[0]
    rest.status = "skipped"
    await session.commit()
    await repository.add_strength_workouts(
        session, plan, {"mon": {"id": 931013083, "name": "Day 1"}})
    ws = await repository.list_workouts(session, plan.id)
    assert {w.type for w in ws} == {"rest", "strength"}


async def test_prune_redundant_rest_is_idempotent_and_reports_zero(session):
    plan = await _plan_with_rest(session)
    await repository.append_workouts(session, plan, [
        PlanWorkout(date="2026-07-06", week=1, type="rest", dist_km=None, description="r"),
        PlanWorkout(date="2026-07-06", week=1, type="easy", dist_km=4.0, description="e"),
    ])
    assert await repository.prune_redundant_rest(session, plan.id) == 0   # already pruned
    assert len(await repository.list_workouts(session, plan.id)) == 1


async def test_daily_job_heals_a_plan_written_before_the_fix(session):
    """Plans that already carry such a row are never written to again — the once-a-day
    plan_sync_job cleans them up (pure DB, no Garmin, no Claude)."""
    from types import SimpleNamespace

    from bot.jobs import _prune_plan_for_user

    plan = await _plan_with_rest(session)
    await repository.append_workouts(session, plan, [
        PlanWorkout(date="2026-07-06", week=1, type="easy", dist_km=4.0, description="e")])
    # written straight to the DB, bypassing the (now pruning) write paths
    from app.db.models import PlannedWorkout
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date="2026-07-06",
                               type="rest", description="бігу немає", status="planned"))
    await session.commit()

    await _prune_plan_for_user(session, SimpleNamespace(id=U1))
    ws = await repository.list_workouts(session, plan.id)
    assert [w.type for w in ws] == ["easy"]


async def test_modify_swapping_a_run_and_a_strength_day_clears_the_other_kind(session):
    """The reported break, end to end. The athlete swapped Wednesday's strength session with
    Thursday's run; the model turned that into two ``modify`` ops, and the write path kept
    each day's old columns. Wednesday then held a ``garmin_template_id`` while calling itself
    an easy run — push cloned the template and named it "🏋️ <the run's description>", so the
    watch showed hanging leg raises under "дуже легкий відновлювальний біг 3 км" — and
    Thursday's strength day still carried the run's 4.5 km, which ``/plan`` duly rendered."""
    plan = await _seed_plan(session)
    await repository.apply_plan_ops(session, plan, [
        PlanOp(action="add", date="2026-07-08", type="strength", description="Day 1",
               garmin_template_id=931013083),
        PlanOp(action="add", date="2026-07-09", type="easy", dist_km=4.5, description="легкий",
               steps=[PlanStep(kind="run", dist_m=4500, hr_zone=2)]),
    ])
    # the swap: Wednesday becomes the run, Thursday becomes the strength day
    await repository.apply_plan_ops(session, plan, [
        PlanOp(action="modify", date="2026-07-08", type="easy", dist_km=3.0,
               description="Дуже легкий відновлювальний біг 3 км",
               steps=[PlanStep(kind="run", dist_m=3000, hr_zone=2)]),
        PlanOp(action="modify", date="2026-07-09", type="strength", description="Day 1",
               garmin_template_id=931013083),
    ])
    by_date = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    run = by_date["2026-07-08"]
    assert run.type == "easy" and run.dist_km == 3.0
    assert run.garmin_template_id is None and run.strength_plan is None
    strength = by_date["2026-07-09"]
    assert strength.type == "strength" and strength.garmin_template_id == 931013083
    assert strength.dist_km is None and strength.steps is None


async def test_modify_to_a_run_drops_a_generated_strength_session(session):
    plan = await _seed_plan(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-07-02", type="strength", description="Ноги",
        strength={"name": "Ноги", "blocks": [{"reps": 3, "exercises": [
            {"category": "squat", "reps": 12}]}]})])
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="modify", date="2026-07-02", type="easy", dist_km=5.0, description="легкий")])
    w = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-02"]
    assert w.type == "easy" and w.dist_km == 5.0 and w.strength_plan is None


async def test_modify_with_strength_content_but_no_type_becomes_a_strength_day(session):
    """A ``modify`` that hands over a strength session without saying ``type="strength"``.
    Taken literally it produces the broken shape (a run-typed row holding a template), so the
    content decides the type instead of quietly contradicting it."""
    plan = await _seed_plan(session)   # 2026-07-01 is an easy run
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="modify", date="2026-07-01", description="Day 1",
        garmin_template_id=931013083)])
    w = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-01"]
    assert w.type == "strength" and w.garmin_template_id == 931013083
    assert w.dist_km is None and w.steps is None


async def test_add_strength_op_never_keeps_a_distance(session):
    plan = await _seed_plan(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-07-02", type="strength", description="Day 1",
        dist_km=4.5, garmin_template_id=931013083)])   # a distance on a strength day
    w = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-02"]
    assert w.type == "strength" and w.dist_km is None and w.steps is None


async def test_swap_exercise_is_ignored_on_a_run(session):
    plan = await _seed_plan(session)   # 2026-07-01 is an easy run
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="swap_exercise", date="2026-07-01",
        from_category="HYPEREXTENSION", to_category="DEADLIFT")])
    w = {x.date: x for x in await repository.list_workouts(session, plan.id)}["2026-07-01"]
    assert w.exercise_edits is None


async def _dated_plan(session, rows):
    """A plan whose rows are inserted in the given order — the ids follow that order, which
    is what the swap bug turned on."""
    from app.db.models import PlannedWorkout, TrainingPlan
    plan = TrainingPlan(user_id=U1, goal="g", status="active")
    session.add(plan)
    await session.flush()
    for r in rows:
        session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, week=7,
                                   status="planned", **r))
    await session.commit()
    return plan


async def test_swapping_two_days_with_moves_actually_swaps_them(session):
    """Two ``move`` ops onto each other's dates — how "поміняй місцями середу і пʼятницю"
    comes back from the model. Resolved lazily, the second move re-found the row the first
    had just moved (``workout_on_date`` takes the lowest id on a date) and moved it straight
    back: for two run days, where the earlier date holds the lower id, the swap did nothing
    whatsoever and the athlete was told it had been applied."""
    plan = await _dated_plan(session, [
        dict(date="2026-08-26", type="easy", dist_km=3.0, description="A"),
        dict(date="2026-08-28", type="tempo", dist_km=8.0, description="B"),
    ])
    await repository.apply_plan_ops(session, plan, [
        PlanOp(action="move", date="2026-08-26", to_date="2026-08-28"),
        PlanOp(action="move", date="2026-08-28", to_date="2026-08-26"),
    ])
    by_date = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert by_date["2026-08-26"].description == "B" and by_date["2026-08-26"].dist_km == 8.0
    assert by_date["2026-08-28"].description == "A" and by_date["2026-08-28"].dist_km == 3.0


async def test_swapping_a_run_and_a_strength_day_with_moves(session):
    """The same swap across the two kinds, in the row order plan generation produces (runs
    first, then ``add_strength_workouts``) and in the opposite one — it used to come out
    right in one and be a no-op in the other."""
    for rows in ([dict(date="2026-08-27", type="easy", dist_km=4.5, description="run"),
                  dict(date="2026-08-26", type="strength", description="Day 1",
                       garmin_template_id=931013083)],
                 [dict(date="2026-08-26", type="strength", description="Day 1",
                       garmin_template_id=931013083),
                  dict(date="2026-08-27", type="easy", dist_km=4.5, description="run")]):
        plan = await _dated_plan(session, rows)
        await repository.apply_plan_ops(session, plan, [
            PlanOp(action="move", date="2026-08-26", to_date="2026-08-27"),
            PlanOp(action="move", date="2026-08-27", to_date="2026-08-26"),
        ])
        by_date = {w.date: w for w in await repository.list_workouts(session, plan.id)}
        assert by_date["2026-08-26"].type == "easy"
        assert by_date["2026-08-27"].type == "strength"
        # each day keeps its own structure — that is the point of expressing a swap as moves
        assert by_date["2026-08-26"].dist_km == 4.5
        assert by_date["2026-08-27"].garmin_template_id == 931013083
        assert by_date["2026-08-27"].dist_km is None


async def test_an_op_still_finds_a_session_added_in_the_same_batch(session):
    """Pre-resolving targets must not blind a later op to a row an earlier ``add`` created."""
    plan = await _dated_plan(session, [
        dict(date="2026-08-26", type="easy", dist_km=3.0, description="A"),
    ])
    await repository.apply_plan_ops(session, plan, [
        PlanOp(action="add", date="2026-08-29", type="easy", dist_km=6.0, description="new"),
        PlanOp(action="modify", date="2026-08-29", description="edited"),
    ])
    by_date = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert by_date["2026-08-29"].description == "edited"
