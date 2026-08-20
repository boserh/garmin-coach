"""A planned session's headline dist_km and its structured steps must tell the same story
(``app.plansteps``) — the pure maths plus the write path that used to let them drift.

The bug this locks down: an adaptation that eases a long run ("зменшую до 5 км") sends a
`modify` with dist_km and a new description but no steps, so the row showed 5.0 km while its
only step still said 6000 m — and since ``workout_export`` builds from steps, the run pushed
to the watch was the un-eased original."""
from contextlib import asynccontextmanager

import pytest

from app import cli, plansteps
from app.db.models import PlannedWorkout, TrainingPlan, User
from app.garmin import repository
from app.garmin.schemas import PlanOp, PlanStep

U1 = 1

EASY = [{"kind": "run", "dist_m": 6000, "hr_zone": 2}]
STRUCTURED = [
    {"kind": "warmup", "dist_m": 1500},
    {"kind": "run", "dist_m": 5000, "pace_min_km": [5.2, 5.4]},
    {"kind": "cooldown", "dist_m": 1500},
]
INTERVALS = [
    {"kind": "warmup", "dist_m": 1000},
    {"kind": "repeat", "reps": 4, "steps": [{"kind": "run", "dist_m": 800},
                                            {"kind": "recovery", "dist_m": 200}]},
    {"kind": "cooldown", "dist_m": 1000},
]
# The shape that exposed the bug below: the work is prescribed in TIME, so only the
# warmup/cooldown carry metres — 3000 m of steps under a wholly correct 6.0 km headline.
TIMED_INTERVALS = [
    {"kind": "warmup", "dist_m": 1500},
    {"kind": "repeat", "reps": 5, "steps": [{"kind": "run", "dur_s": 120,
                                             "pace_min_km": [5.92, 6.25]},
                                            {"kind": "recovery", "dur_s": 120}]},
    {"kind": "cooldown", "dist_m": 1500},
]


# ---------- pure maths ----------

def test_total_counts_repeats_and_reports_none_for_timed_steps():
    assert plansteps.total_dist_m(EASY) == 6000
    assert plansteps.total_dist_m(INTERVALS) == 1000 + 4 * 1000 + 1000
    # a purely time-based session has no distance to reconcile — None, not 0
    assert plansteps.total_dist_m([{"kind": "run", "dur_s": 1800, "hr_zone": 2}]) is None
    assert plansteps.total_dist_m([]) is None


def test_timed_work_means_the_steps_do_not_describe_the_distance():
    """A session mixing metres and minutes is only PARTLY described in metres."""
    assert plansteps.describes_distance(EASY) and plansteps.describes_distance(INTERVALS)
    assert plansteps.total_dist_m(TIMED_INTERVALS) == 3000      # the sum is still a sum...
    assert not plansteps.describes_distance(TIMED_INTERVALS)    # ...but not the session
    assert not plansteps.describes_distance([{"kind": "run", "dur_s": 1800}])


def test_a_correct_headline_over_timed_work_is_neither_flagged_nor_overwritten():
    """The reported false alarm: 'розминка 1.5 км + 5×2 хв + заминка 1.5 км' really is
    ~6 km, and the 3000 m of distance steps is no evidence against it."""
    assert plansteps.mismatch(6.0, TIMED_INTERVALS) is None
    assert plansteps.reconcile(6.0, TIMED_INTERVALS, steps_given=True) == (6.0, TIMED_INTERVALS)
    assert plansteps.reconcile(6.0, TIMED_INTERVALS, steps_given=False) == (6.0, TIMED_INTERVALS)


def test_timed_work_is_never_rescaled():
    # scaling to 5 km would shrink the warmup/cooldown and leave the intervals — the very
    # opposite of "a coach who cuts volume cuts the work"
    assert plansteps.scale_steps(TIMED_INTERVALS, 5.0) is None


def test_scale_puts_the_change_on_the_work_and_keeps_warmup():
    out = plansteps.scale_steps(STRUCTURED, 6.0)
    assert plansteps.total_dist_m(out) == 6000
    assert out[0]["dist_m"] == 1500 and out[2]["dist_m"] == 1500  # warmup/cooldown untouched
    assert out[1]["dist_m"] == 3000                               # the work absorbed the cut


def test_scale_falls_back_to_proportional_when_the_fixed_parts_do_not_fit():
    # 2 km target against a 1.5 + 1.5 km warmup/cooldown: keeping them intact is impossible
    out = plansteps.scale_steps(STRUCTURED, 2.0)
    assert plansteps.total_dist_m(out) == 2000
    assert out[0]["dist_m"] < 1500 and out[1]["dist_m"] > 0


def test_scale_keeps_repeat_structure():
    out = plansteps.scale_steps(INTERVALS, 4.0)
    assert plansteps.total_dist_m(out) == 4000
    rep = out[1]
    assert rep["kind"] == "repeat" and rep["reps"] == 4        # reps never change, distances do
    assert rep["steps"][1]["dist_m"] == 200                    # recovery is not work — kept


def test_scale_declines_when_there_is_nothing_to_do():
    assert plansteps.scale_steps(EASY, 6.05) is None            # inside tolerance
    assert plansteps.scale_steps(EASY, 0) is None
    assert plansteps.scale_steps([{"kind": "run", "dur_s": 600}], 3.0) is None
    assert plansteps.scale_steps(None, 3.0) is None


def test_reconcile_lets_fresh_steps_define_the_distance():
    # both written together (a generation, or an edit that sent both) — steps reach the
    # watch, so they win and the headline follows
    dist, steps = plansteps.reconcile(5.0, EASY, steps_given=True)
    assert dist == 6.0 and steps == EASY


def test_reconcile_recuts_stale_steps_to_a_new_distance():
    # only the distance was written (the coach's decision); the steps are the row's old ones
    dist, steps = plansteps.reconcile(5.0, EASY, steps_given=False)
    assert dist == 5.0 and plansteps.total_dist_m(steps) == 5000


def test_reconcile_fills_a_missing_distance_and_leaves_agreeing_ones_alone():
    assert plansteps.reconcile(None, EASY, steps_given=False)[0] == 6.0
    assert plansteps.reconcile(6.0, EASY, steps_given=False) == (6.0, EASY)
    assert plansteps.reconcile(5.0, None, steps_given=True) == (5.0, None)


# ---------- the write path ----------

async def _seed(session, *, dist_km=6.0, steps=None):
    plan = TrainingPlan(user_id=U1, goal="g", status="active", start_date="2026-08-01")
    session.add(plan)
    await session.flush()
    session.add(PlannedWorkout(
        plan_id=plan.id, user_id=U1, date="2026-08-08", type="long",
        dist_km=dist_km, description="довгий", steps=steps if steps is not None else EASY,
        status="planned"))
    await session.commit()
    return plan


async def _workout(session, plan):
    return (await repository.list_workouts(session, plan.id))[0]


async def test_modify_easing_only_the_distance_recuts_the_steps(session):
    """The reported bug: header 5.0 km, step still 6.0 km, watch gets the full session."""
    plan = await _seed(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="modify", date="2026-08-08", dist_km=5.0,
        description="Легкий рівномірний біг у зоні 2, повільніше за звичне")])
    w = await _workout(session, plan)
    assert w.dist_km == 5.0
    assert plansteps.total_dist_m(w.steps) == 5000
    assert w.steps[0]["hr_zone"] == 2          # the target survives the rescale


async def test_modify_with_steps_makes_the_headline_follow_them(session):
    plan = await _seed(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="modify", date="2026-08-08", dist_km=5.0,
        steps=[PlanStep(kind="run", dist_m=4000, hr_zone=2)])])
    w = await _workout(session, plan)
    assert w.dist_km == 4.0 and plansteps.total_dist_m(w.steps) == 4000


async def test_modify_that_touches_neither_leaves_the_pair_alone(session):
    plan = await _seed(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="modify", date="2026-08-08", description="інший текст")])
    w = await _workout(session, plan)
    assert w.dist_km == 6.0 and w.steps == EASY


async def test_add_with_disagreeing_numbers_is_stored_consistent(session):
    plan = await _seed(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-08-10", type="easy", dist_km=5.0, description="x",
        steps=[PlanStep(kind="run", dist_m=6000, hr_zone=2)])])
    ws = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert ws["2026-08-10"].dist_km == 6.0


async def test_add_with_timed_work_keeps_the_models_headline(session):
    """The extension job logged 'dist_km=6.0 steps=3000m (50%) — steps win' and stored 3.0.
    Nothing was wrong with the 6.0: three of its kilometres are simply prescribed in minutes."""
    plan = await _seed(session)
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="add", date="2026-09-29", type="intervals", dist_km=6.0,
        description="Розминка 1.5 км. Потім 5×2 хв у зусиллі. Заминка 1.5 км.",
        steps=[PlanStep(kind="warmup", dist_m=1500),
               PlanStep(kind="repeat", reps=5,
                        steps=[PlanStep(kind="run", dur_s=120),
                               PlanStep(kind="recovery", dur_s=120)]),
               PlanStep(kind="cooldown", dist_m=1500)])])
    ws = {w.date: w for w in await repository.list_workouts(session, plan.id)}
    assert ws["2026-09-29"].dist_km == 6.0


async def test_timed_session_keeps_its_distance_untouched(session):
    """No distance in the steps means nothing to reconcile — don't invent one."""
    plan = await _seed(session, steps=[{"kind": "run", "dur_s": 1800, "hr_zone": 2}])
    await repository.apply_plan_ops(session, plan, [PlanOp(
        action="modify", date="2026-08-08", dist_km=5.0)])
    w = await _workout(session, plan)
    assert w.dist_km == 5.0 and w.steps == [{"kind": "run", "dur_s": 1800, "hr_zone": 2}]


# ---------- repairing rows written before the fix ----------

@pytest.fixture
def _cli_session(session, monkeypatch):
    """Route app.cli's async_session_maker/init_db to the test in-memory session."""
    @asynccontextmanager
    async def maker():
        yield session

    async def _noop_init_db():
        return None

    monkeypatch.setattr(cli, "async_session_maker", maker)
    monkeypatch.setattr(cli, "init_db", _noop_init_db)
    return session


async def _seed_broken(session, *, pushed=False):
    user = User(email="fix@x.com", password_hash="h")
    session.add(user)
    await session.commit()
    plan = TrainingPlan(user_id=user.id, goal="g", status="active", start_date="2026-08-01")
    session.add(plan)
    await session.flush()
    session.add(PlannedWorkout(
        plan_id=plan.id, user_id=user.id, date="2026-08-08", type="long",
        dist_km=5.0, description="довгий", steps=EASY, status="planned",
        garmin_workout_id=99 if pushed else None))
    await session.commit()
    return user, plan


async def test_fix_plan_steps_is_a_dry_run_by_default(_cli_session, capsys):
    session = _cli_session
    _user, plan = await _seed_broken(session)
    assert await cli._fix_plan_steps("fix@x.com", apply=False) == 0
    out = capsys.readouterr().out
    assert "6000m → 5000m" in out and "--apply" in out
    w = (await repository.list_workouts(session, plan.id))[0]
    assert w.steps == EASY               # untouched without --apply


async def test_fix_plan_steps_applies_and_flags_the_pushed_ones(_cli_session, capsys):
    session = _cli_session
    _user, plan = await _seed_broken(session, pushed=True)
    assert await cli._fix_plan_steps("fix@x.com", apply=True) == 0
    out = capsys.readouterr().out
    # a session already on the calendar carries the OLD workout — say so, don't silently fix
    assert "needs a re-push" in out and "unpush-plan" in out
    w = (await repository.list_workouts(session, plan.id))[0]
    assert plansteps.total_dist_m(w.steps) == 5000
