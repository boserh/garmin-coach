"""NF-30: the return-to-run protocol — the ladder, the stop rule, the guard, the job branch.

The protocol is deterministic by design (an AC: zero LLM calls anywhere in it), so most of
this is pure-function testing. The two things that are really being defended here are the
stop rule — a body that keeps saying no must stop the progression, not be pushed through it —
and the medical boundary: this feature describes load and never names an injury.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import returntorun
from app.core.config import settings
from app.db.models import PlannedWorkout, TrainingPlan, User

TODAY = dt.date(2026, 8, 5)


def _runs(*pains):
    """EP-12 check-ins, oldest-first; each item is the `pain` flag."""
    return [{"date": f"2026-07-{20 + i:02d}", "pain": p, "note": "коліно" if p else None}
            for i, p in enumerate(pains)]


# ---------- trigger ----------

def test_one_painful_run_is_not_a_trigger():
    """The ticket's own risk note: a single "yes it hurt" after a stumble is not a pattern."""
    assert returntorun.should_offer(_runs(False, False, True)) is None


def test_pain_on_several_recent_runs_offers_the_protocol():
    t = returntorun.should_offer(_runs(False, True, False, True, False))
    assert t["pain_runs"] == 2
    assert t["note"] == "коліно"


def test_old_pain_outside_the_window_does_not_trigger():
    assert returntorun.should_offer(
        _runs(True, True, False, False, False, False, False)) is None


# ---------- the ladder ----------

def test_pain_within_the_limit_steps_up():
    s = returntorun.advance(returntorun.start(TODAY), 1, today=TODAY)
    assert s["outcome"] == "up" and s["step"] == 2


def test_pain_above_the_limit_repeats_the_same_step():
    s = returntorun.advance(returntorun.start(TODAY), 5, today=TODAY)
    assert s["outcome"] == "repeat" and s["step"] == 1
    assert s["repeats"] == 1


def test_a_day_without_a_run_moves_nothing():
    """An AC: a missed day moves the step neither up nor down. Silence is not evidence."""
    before = returntorun.advance(returntorun.start(TODAY), 1, today=TODAY)
    after = returntorun.advance(before, None, today=TODAY + dt.timedelta(days=1))
    assert after["outcome"] == "idle"
    assert after["step"] == before["step"]
    assert after["repeats"] == before["repeats"]


def test_rising_pain_twice_in_a_row_stops_the_protocol():
    """An AC, and the reason the feature exists: a second injury from the same cause is the
    most expensive outcome of a season."""
    s = returntorun.start(TODAY)
    s = returntorun.advance(s, 4, today=TODAY)                          # hurt
    assert s["outcome"] == "repeat"
    s = returntorun.advance(s, 5, today=TODAY + dt.timedelta(days=2))   # worse
    assert s["outcome"] == "repeat"
    s = returntorun.advance(s, 6, today=TODAY + dt.timedelta(days=4))   # worse again
    assert s["outcome"] == "stop" and s["status"] == "stopped"
    # ...and it stops proposing progression from then on.
    after = returntorun.advance(s, 0, today=TODAY + dt.timedelta(days=4))
    assert after["outcome"] == "idle" and after["status"] == "stopped"


def test_pain_that_stays_high_but_does_not_rise_keeps_repeating():
    """Repeating is not the same as stopping: steady discomfort at the same level means the
    rung wasn't cleared, not that something is getting worse."""
    s = returntorun.start(TODAY)
    for _ in range(3):
        s = returntorun.advance(s, 4, today=TODAY)
    assert s["status"] == "active" and s["step"] == 1


def test_clearing_the_last_rung_finishes_the_protocol():
    s = returntorun.start(TODAY)
    for _ in range(returntorun.LAST_STEP - 1):
        s = returntorun.advance(s, 0, today=TODAY)
    assert s["step"] == returntorun.LAST_STEP and s["status"] == "active"
    s = returntorun.advance(s, 0, today=TODAY)
    assert s["outcome"] == "done" and s["status"] == "done"


def test_the_ladder_actually_progresses_from_walking_to_running():
    run_minutes = [s["run_min"] for s in returntorun.STEPS]
    assert run_minutes[0] == 0                       # starts at walking
    assert run_minutes == sorted(run_minutes)        # and never goes backwards
    assert returntorun.STEPS[-1]["walk_min"] == 0    # ends on continuous running


# ---------- the session ----------

def test_walk_run_sessions_use_the_existing_step_shapes():
    """No new DTO (the ticket is explicit): walk/run is ordinary structured interval steps,
    so the existing Garmin push handles it unchanged."""
    steps = returntorun.session_steps(returntorun.step_by_number(2))
    kinds = {s["kind"] for s in steps}
    assert kinds <= {"warmup", "run", "recovery", "cooldown", "repeat"}
    repeat = next(s for s in steps if s["kind"] == "repeat")
    assert repeat["reps"] == 5
    assert {s["kind"] for s in repeat["steps"]} == {"run", "recovery"}


def test_the_first_step_is_walking_only():
    steps = returntorun.session_steps(returntorun.step_by_number(1))
    assert [s["kind"] for s in steps] == ["recovery"]


# ---------- the medical boundary ----------

_BANNED = ("тендиніт", "діагноз", "розтягнення", "запалення", "синдром", "перелом",
           "потерпи", "не звертай уваги")


def test_no_text_in_the_protocol_names_an_injury_or_pushes_through_pain():
    """A product boundary, not a technical one — so it's a test, not a convention."""
    texts = [returntorun.offer_text({"pain_runs": 2, "window": 5, "note": "коліно"})]
    state = returntorun.start(TODAY)
    texts.append(returntorun.step_text(state))
    for pain in (0, 4, 6):
        state = returntorun.advance(state, pain, today=TODAY)
        texts.append(returntorun.outcome_text(state) or "")
    blob = " ".join(texts).lower()
    for banned in _BANNED:
        assert banned not in blob, banned


def test_the_stop_rule_points_at_a_professional():
    s = returntorun.advance(returntorun.start(TODAY), 4, today=TODAY)
    s = returntorun.advance(s, 6, today=TODAY)
    s = returntorun.advance(s, 7, today=TODAY)
    assert s["status"] == "stopped"
    assert "фахівця" in returntorun.outcome_text(s)


# ---------- storage / bot wiring ----------

async def _user(session, email="rtr@example.com") -> User:
    u = User(email=email, password_hash="x", is_active=True, is_approved=True,
             telegram_chat_id=555, alerts_enabled=True)
    session.add(u)
    await session.flush()
    return u


@pytest.mark.asyncio
async def test_a_paused_plan_stays_the_current_plan_and_keeps_its_sessions(session):
    """An AC: pausing is NOT archiving — /plan still shows it and its future sessions
    survive, so the protocol can hand the plan back afterwards."""
    from app.garmin import repository

    user = await _user(session, "paused@example.com")
    plan = TrainingPlan(user_id=user.id, goal="first_10k", status="active",
                        start_date="2026-08-01")
    session.add(plan)
    await session.flush()
    session.add(PlannedWorkout(plan_id=plan.id, user_id=user.id, date="2026-08-20",
                               type="long", dist_km=15.0, status="planned"))
    await session.flush()

    await repository.set_plan_paused(session, plan, True)
    await session.flush()

    current = await repository.get_active_plan(session, user.id)
    assert current is not None and current.id == plan.id and current.status == "paused"
    assert len(await repository.list_workouts(session, plan.id)) == 1

    await repository.set_plan_paused(session, plan, False)
    assert (await repository.get_active_plan(session, user.id)).status == "active"


@pytest.mark.asyncio
async def test_declining_the_offer_changes_nothing_and_snoozes(session):
    """An AC: ❌ must leave the plan completely untouched and stay quiet for
    RETURN_GUARD_DAYS."""
    from app.garmin import repository
    from bot import handlers as h

    user = await _user(session, "declined@example.com")
    plan = TrainingPlan(user_id=user.id, goal="first_10k", status="active",
                        start_date="2026-08-01")
    session.add(plan)
    await session.flush()

    q = SimpleNamespace(data="rtr:no", answer=AsyncMock(), edit_message_text=AsyncMock(),
                        message=SimpleNamespace(chat=SimpleNamespace(id=555)))
    update = SimpleNamespace(callback_query=q)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield session

    with patch.object(h, "async_session_maker", _cm):
        await h.return_callback(update, SimpleNamespace())

    assert plan.status == "active"
    assert await h.load_return_state(session, user.id) is None
    assert await repository.get_state(session, user.id, h.RETURN_WARNED_KEY)


@pytest.mark.asyncio
async def test_accepting_pauses_the_plan_and_schedules_the_first_rung(session):
    from app.garmin import repository
    from bot import handlers as h

    user = await _user(session, "accepted@example.com")
    plan = TrainingPlan(user_id=user.id, goal="first_10k", status="active",
                        start_date="2026-08-01")
    session.add(plan)
    await session.flush()

    q = SimpleNamespace(data="rtr:yes", answer=AsyncMock(), edit_message_text=AsyncMock(),
                        message=SimpleNamespace(chat=SimpleNamespace(id=555)))
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield session

    with patch.object(h, "async_session_maker", _cm):
        await h.return_callback(SimpleNamespace(callback_query=q), SimpleNamespace())

    assert plan.status == "paused"
    state = await h.load_return_state(session, user.id)
    assert state["status"] == "active" and state["step"] == 1
    workouts = await repository.list_workouts(session, plan.id)
    assert any((w.description or "").startswith("Крок 1/") for w in workouts)
    session_row = next(w for w in workouts if (w.description or "").startswith("Крок 1/"))
    assert session_row.steps, "the rung must be pushable to the watch"


@pytest.mark.asyncio
async def test_the_daily_check_asks_nothing_when_nothing_was_run(session):
    """An AC restated at the job level: a day the runner didn't run produces no question and
    moves no step."""
    from bot import handlers as h
    from bot import jobs as jobs_mod

    user = await _user(session, "noruns@example.com")
    await h.save_return_state(session, user.id, returntorun.start(TODAY))
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    sent = await jobs_mod._return_to_run_check_for_user(
        ctx, session, user, dt.date.today().isoformat())

    assert sent is False
    ctx.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_master_switch_turns_the_whole_feature_off(session, monkeypatch):
    """The feature closest to the medical boundary gets an outright off switch."""
    from bot import jobs as jobs_mod

    monkeypatch.setattr(settings, "RETURN_TO_RUN", False)
    user = await _user(session, "off@example.com")
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    assert await jobs_mod._return_to_run_check_for_user(
        ctx, session, user, dt.date.today().isoformat()) is False
    ctx.bot.send_message.assert_not_awaited()
