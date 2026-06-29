"""Training-plan generation: JSON coercion + persistence (Claude mocked)."""
from unittest.mock import patch

from app.analysis import service
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
    with patch.object(service, "generate_plan_with_stats",
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
    rep = _fmt_step({"kind": "repeat", "reps": 5, "steps": [
        {"kind": "run", "dur_s": 180, "pace_min_km": [5.25, 5.4]},
        {"kind": "recovery", "dur_s": 120, "pace_min_km": None}]})
    assert rep == "5× (біг 3 хв @ 5:15–5:24/км + відновлення 2 хв)"


def test_coerce_edit_parses():
    e = _coerce_edit(
        '{"summary": "додаю", "operations": [{"action": "add", "date": "2026-07-02", '
        '"type": "easy", "dist_km": 5.0, "description": "легко"}]}'
    )
    assert e.summary == "додаю" and e.operations[0].action == "add"


async def _seed_plan(session):
    with patch.object(service, "generate_plan_with_stats",
                      return_value=(_gen(), CallStats(kind="plan", model="m"))):
        return await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="x", target_date=None,
            start_date="2026-06-25", days_per_week=3, intensity="easy", intake={}, api_key=None)


async def test_apply_plan_ops(session):
    plan = await _seed_plan(session)  # workouts on 2026-07-01 (easy) + 2026-07-03 (rest)
    n = await repository.apply_plan_ops(session, plan, [
        PlanOp(action="add", date="2026-07-02", type="easy", dist_km=5.0, description="новий"),
        PlanOp(action="modify", date="2026-07-01", dist_km=6.0),
        PlanOp(action="move", date="2026-07-03", to_date="2026-07-04"),
    ])
    assert n == 3
    by_date = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert by_date["2026-07-02"].description == "новий"
    assert by_date["2026-07-01"].dist_km == 6.0
    assert "2026-07-04" in by_date and "2026-07-03" not in by_date

    await repository.apply_plan_ops(session, plan, [PlanOp(action="skip", date="2026-07-01")])
    by_date = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert by_date["2026-07-01"].status == "skipped"


async def test_run_plan_edit_proposes_without_applying(session):
    plan = await _seed_plan(session)
    edit = PlanEdit(summary="додаю біг", operations=[
        PlanOp(action="add", date="2026-07-02", type="easy", dist_km=5.0, description="легко")])
    with patch.object(service, "plan_edit_with_stats",
                      return_value=(edit, CallStats(kind="plan_edit", model="m"))):
        _plan, out = await run_plan_edit(
            session, user_id=U1, instruction="додай біг 2 липня", api_key=None)
    assert out.summary == "додаю біг"
    # proposed only — not yet written
    assert all(w.date != "2026-07-02" for w in await repository.list_workouts(session, plan.id))
