"""EP-12 phases 2-3: the cross-run subjective aggregation (app.subjective) that feeds the
report/adaptation contexts, plus the digest's plan/fact "overreached" flag computed in
repository.weekly_compliance."""
import pytest

from app import subjective
from app.db.models import ActivityRecord, PlannedWorkout, TrainingPlan
from app.garmin import repository

U1 = 1


def _run(date, *, pace=6.0, rpe=None, pain=False, note=None):
    return {"date": date, "pace": pace, "rpe": rpe, "pain": pain, "note": note}


# ---- recurring_pain --------------------------------------------------------

def test_recurring_pain_none_when_under_threshold():
    runs = [_run("2026-07-01", pain=True, note="коліно")]
    assert subjective.recurring_pain(runs) is None


def test_recurring_pain_same_part_twice():
    runs = [_run("2026-07-01", pain=True, note="Коліно"),
            _run("2026-07-05", pain=True, note="коліно")]
    assert subjective.recurring_pain(runs) == {"part": "коліно", "count": 2}


def test_recurring_pain_picks_most_repeated():
    runs = [_run("2026-07-01", pain=True, note="стопа"),
            _run("2026-07-02", pain=True, note="коліно"),
            _run("2026-07-03", pain=True, note="коліно"),
            _run("2026-07-04", pain=True, note="коліно")]
    assert subjective.recurring_pain(runs) == {"part": "коліно", "count": 3}


def test_recurring_pain_ignores_runs_without_pain_flag():
    # A note without the pain flag doesn't count.
    runs = [_run("2026-07-01", note="коліно"), _run("2026-07-02", note="коліно")]
    assert subjective.recurring_pain(runs) is None


def test_recurring_pain_no_note_uses_generic_bucket():
    runs = [_run("2026-07-01", pain=True), _run("2026-07-02", pain=True)]
    assert subjective.recurring_pain(runs) == {"part": "біль", "count": 2}


# ---- rpe_rising ------------------------------------------------------------

def test_rpe_rising_true_when_effort_climbs_at_stable_pace():
    runs = [_run("2026-07-01", pace=6.0, rpe=4), _run("2026-07-02", pace=6.0, rpe=4),
            _run("2026-07-05", pace=6.05, rpe=7), _run("2026-07-06", pace=6.0, rpe=7)]
    assert subjective.rpe_rising(runs) is True


def test_rpe_rising_false_when_pace_also_got_faster():
    # RPE up but so is the pace (much faster) — that explains the effort away.
    runs = [_run("2026-07-01", pace=7.0, rpe=4), _run("2026-07-02", pace=7.0, rpe=4),
            _run("2026-07-05", pace=5.5, rpe=7), _run("2026-07-06", pace=5.5, rpe=7)]
    assert subjective.rpe_rising(runs) is False


def test_rpe_rising_false_without_enough_rated_runs():
    runs = [_run("2026-07-01", rpe=4), _run("2026-07-02", rpe=8)]
    assert subjective.rpe_rising(runs) is False


def test_rpe_rising_false_when_flat():
    runs = [_run("2026-07-01", rpe=5), _run("2026-07-02", rpe=5),
            _run("2026-07-05", rpe=5), _run("2026-07-06", rpe=6)]
    assert subjective.rpe_rising(runs) is False


# ---- summarize -------------------------------------------------------------

def test_summarize_none_without_checkins():
    assert subjective.summarize([_run("2026-07-01")]) is None


def test_summarize_shapes_the_signal():
    runs = [_run("2026-07-01", pace=6.0, rpe=4),
            _run("2026-07-02", pace=6.0, rpe=5),
            _run("2026-07-05", pace=6.0, rpe=7, pain=True, note="Коліно"),
            _run("2026-07-06", pace=6.0, rpe=7, pain=True, note="коліно")]
    out = subjective.summarize(runs)
    assert out["n"] == 4
    assert out["avg_rpe"] == pytest.approx(5.8)
    assert out["rpe_rising"] is True
    assert out["recurring_pain"] == {"part": "коліно", "count": 2}
    # recent entries drop null/empty keys and normalise the note.
    assert out["recent"][-1] == {"date": "2026-07-06", "rpe": 7, "pain": True, "note": "коліно"}


def test_summarize_limits_recent_entries():
    runs = [_run(f"2026-07-{d:02d}", rpe=5) for d in range(1, 12)]
    out = subjective.summarize(runs, limit=3)
    assert len(out["recent"]) == 3
    assert out["n"] == 11  # n counts all check-ins, recent is only the tail


def test_summarize_no_recurring_pain_key_when_absent():
    out = subjective.summarize([_run("2026-07-01", rpe=6)])
    assert "recurring_pain" not in out


# ---- weekly_compliance overreached (digest phase 3) ------------------------

async def _plan(session):
    plan = TrainingPlan(user_id=U1, goal="first_5k", status="active",
                        start_date="2026-07-01", target_date="2026-09-01")
    session.add(plan)
    await session.flush()
    return plan


async def _done_workout(session, plan, date, *, type_, activity_id):
    w = PlannedWorkout(plan_id=plan.id, user_id=U1, date=date, week=1, type=type_,
                       dist_km=5.0, description="", status="done",
                       completed_activity_id=activity_id)
    session.add(w)
    return w


async def _activity(session, aid, *, rpe):
    a = ActivityRecord(id=aid, user_id=U1, activity_id=1000 + aid, date="2026-07-06",
                       type="running", dist_km=5.0, dur_min=30.0,
                       subjective={"rpe": rpe})
    session.add(a)
    return a


async def test_weekly_compliance_flags_overreached_easy_session(session):
    plan = await _plan(session)
    await _activity(session, 1, rpe=9)   # easy run that felt very hard
    await _done_workout(session, plan, "2026-07-06", type_="easy", activity_id=1)
    await session.commit()

    comp = await repository.weekly_compliance(session, plan.id)
    wk = comp["2026-W28"]
    assert wk["done"] == 1
    assert wk["overreached"] == 1


async def test_weekly_compliance_hard_session_not_overreached(session):
    # A tempo at RPE 9 is expected to be hard — not an overreach flag.
    plan = await _plan(session)
    await _activity(session, 2, rpe=9)
    await _done_workout(session, plan, "2026-07-06", type_="tempo", activity_id=2)
    await session.commit()

    comp = await repository.weekly_compliance(session, plan.id)
    assert comp["2026-W28"]["overreached"] == 0


async def test_weekly_compliance_easy_low_rpe_not_overreached(session):
    plan = await _plan(session)
    await _activity(session, 3, rpe=4)
    await _done_workout(session, plan, "2026-07-06", type_="easy", activity_id=3)
    await session.commit()

    comp = await repository.weekly_compliance(session, plan.id)
    assert comp["2026-W28"]["overreached"] == 0


async def test_weekly_compliance_overreached_zero_without_checkin(session):
    plan = await _plan(session)
    a = ActivityRecord(id=4, user_id=U1, activity_id=1004, date="2026-07-06",
                       type="running", dist_km=5.0, dur_min=30.0)  # no subjective
    session.add(a)
    await _done_workout(session, plan, "2026-07-06", type_="easy", activity_id=4)
    await session.commit()

    comp = await repository.weekly_compliance(session, plan.id)
    assert comp["2026-W28"]["overreached"] == 0
