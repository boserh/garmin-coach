"""A session may only be called ``long`` if it IS the week's long run (``app.longrun``) —
the pure rule, the write path that enforces it, and the adaptation guard that let the
mislabelled session in.

The bug this locks down: a plan running 4.0 km easy on Tuesday picked up an *added*
4.0 km "long" on the Wednesday — one day later, and the shortest run of a week whose real
long run (6.0 km) still stood on the Sunday. ``type`` is a behaviour flag (bot.jobs's
ADAPT_HEAVY_TYPES gate the whole weekly/weather review on it), so that label made the coach
defend a routine easy run as a key session.
"""
from types import SimpleNamespace

import pytest

from app import longrun
from app.analysis.plans import _filter_ops_to_level
from app.db.models import PlannedWorkout, TrainingPlan
from app.garmin import repository
from app.garmin.schemas import PlanOp, PlanStep

U1 = 1


def _run(date, type_, dist_km):
    return SimpleNamespace(date=date, type=type_, dist_km=dist_km)


# ---------- the pure rule ----------

def test_the_reported_week_demotes_the_impostor_and_keeps_the_real_long_run():
    week = [_run("2026-09-01", "easy", 4.0),      # Tue
            _run("2026-09-02", "long", 4.0),      # Wed — added, no longer than the easy day
            _run("2026-09-06", "long", 6.0)]      # Sun — 1.5× the easy run, the week's longest
    assert longrun.unearned_long_dates(week) == {"2026-09-02"}


def test_being_the_longest_is_not_enough():
    """4.5 among 4.0s is the same session plus a lap, not a different stimulus."""
    week = [_run("2026-09-01", "easy", 4.0),
            _run("2026-09-03", "easy", 4.0),
            _run("2026-09-06", "long", 4.5)]      # ratio 1.13 — under MIN_RATIO
    assert longrun.unearned_long_dates(week) == {"2026-09-06"}


def test_exactly_the_ratio_earns_the_label():
    week = [_run("2026-09-01", "easy", 4.0), _run("2026-09-06", "long", 5.0)]   # 1.25
    assert longrun.unearned_long_dates(week) == set()


def test_a_long_shorter_than_a_tempo_is_not_the_weeks_longest_run():
    week = [_run("2026-09-01", "easy", 4.0),
            _run("2026-09-03", "tempo", 8.0),
            _run("2026-09-06", "long", 6.0)]      # clears the ratio, but is not the longest
    assert longrun.unearned_long_dates(week) == {"2026-09-06"}


def test_the_baseline_is_easy_running_not_every_run():
    """A 10 km tempo must not raise the bar the long run has to clear."""
    week = [_run("2026-09-01", "easy", 4.0),
            _run("2026-09-03", "tempo", 5.0),
            _run("2026-09-06", "long", 6.0)]      # 6.0 / 4.0 = 1.5 against the EASY median
    assert longrun.unearned_long_dates(week) == set()


def test_nothing_to_compare_against_leaves_the_label_alone():
    """Demote only what can be disproved — a week with no easy run has no baseline."""
    assert longrun.unearned_long_dates([_run("2026-09-06", "long", 4.0)]) == set()
    assert longrun.unearned_long_dates([]) == set()


def test_a_session_without_a_usable_distance_is_never_judged():
    week = [_run("2026-09-01", "easy", 4.0), _run("2026-09-06", "long", None)]
    assert longrun.unearned_long_dates(week) == set()


def test_iso_week_groups_by_the_calendar_not_the_plans_counter():
    assert longrun.iso_week("2026-09-02") == longrun.iso_week("2026-09-06")
    assert longrun.iso_week("2026-09-07") != longrun.iso_week("2026-09-06")
    assert longrun.iso_week(None) is None and longrun.iso_week("nonsense") is None


# ---------- the write path ----------

async def _seed(session):
    """The reported week, minus the session that caused it: Tue easy 4.0, Sun long 6.0."""
    plan = TrainingPlan(user_id=U1, goal="general", status="active",
                        start_date="2026-08-31")
    session.add(plan)
    await session.flush()
    for date, type_, km in (("2026-09-01", "easy", 4.0), ("2026-09-06", "long", 6.0)):
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=U1, date=date, week=7, type=type_, dist_km=km,
            description="x", steps=[{"kind": "run", "dist_m": km * 1000, "hr_zone": 2}],
            status="planned"))
    await session.commit()
    return plan


async def _types(session, plan):
    return {w.date: w.type for w in await repository.list_workouts(session, plan.id)}


async def test_an_added_long_that_is_not_the_weeks_long_run_is_stored_as_easy(session):
    plan = await _seed(session)
    affected = await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-09-02", type="long", dist_km=4.0,
        description="Обережне повернення до довшого бігу",
        steps=[PlanStep(kind="run", dist_m=4000, hr_zone=2)])])
    types = await _types(session, plan)
    assert types["2026-09-02"] == "easy"      # the impostor
    assert types["2026-09-06"] == "long"      # the real one, untouched
    # the row must reach the caller: `type` is part of the workout name pushed to Garmin
    assert "2026-09-02" in {w.date for w in affected}


async def test_easing_the_long_run_below_the_bar_relabels_it(session):
    """A deload that cuts Sunday to 4 km leaves the week without a long run — and the plan
    must say so, rather than keep a 'long' the same length as Tuesday's easy day."""
    plan = await _seed(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="modify", date="2026-09-06", dist_km=4.0)])
    assert (await _types(session, plan))["2026-09-06"] == "easy"


async def test_a_real_long_run_survives_every_write(session):
    plan = await _seed(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="modify", date="2026-09-06", dist_km=7.0)])
    assert (await _types(session, plan))["2026-09-06"] == "long"


async def test_moving_the_long_run_within_its_week_keeps_the_label(session):
    plan = await _seed(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="move", date="2026-09-06", to_date="2026-09-05")])
    types = await _types(session, plan)
    assert types["2026-09-05"] == "long" and "2026-09-06" not in types


async def test_generation_cannot_write_a_mislabelled_long_run_either(session):
    plan = await repository.create_plan(
        session, user_id=U1, goal="general", goal_label="g", target_date=None,
        start_date="2026-09-01", days_per_week=3, intensity="moderate", intake={},
        summary="s",
        workouts=[SimpleNamespace(date="2026-09-01", week=1, type="easy", dist_km=4.0,
                                  description="x", steps=None),
                  SimpleNamespace(date="2026-09-02", week=1, type="long", dist_km=4.0,
                                  description="x", steps=None),
                  SimpleNamespace(date="2026-09-06", week=1, type="long", dist_km=6.0,
                                  description="x", steps=None)])
    types = await _types(session, plan)
    assert types["2026-09-02"] == "easy" and types["2026-09-06"] == "long"


# ---------- the guard that let it in ----------

@pytest.mark.parametrize("level", ["conservative", "flexible"])
def test_adaptation_may_never_invent_a_session(level):
    """SYSTEM_PLAN_ADAPT forbids `add`, but a prompt is not a guard: on a flexible plan the
    level filter used to pass every operation through untouched."""
    ops = [PlanOp(action="add", date="2026-09-02", type="long", dist_km=4.0),
           PlanOp(action="modify", date="2026-09-06", dist_km=5.5)]
    kept = _filter_ops_to_level(ops, level, {"2026-09-06": 6.0}, None)
    assert [op.action for op in kept] == ["modify"]
