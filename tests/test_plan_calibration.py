"""Pace-target calibration: a structured session whose working steps ALL missed on the same
side turns into a concrete ✅/❌ proposal for the upcoming structured sessions (Claude
mocked — 0 real calls).

The report that asked for it (2026-09-30): 5×2' on a 5:55–6:15 target run at 5:05→4:19,
the analysis said "raise the interval targets", and nothing in the plan changed."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import stepmatch
from app.analysis import plans as plans_mod
from app.db.models import ActivityRecord, PlannedWorkout, TrainingPlan, User
from app.garmin import repository
from app.garmin.schemas import PlanEdit, PlanOp, PlanStep
from bot import jobs as jobs_module

TARGET = [5.92, 6.25]


def _step(n, actual, delta):
    return {"step": n, "kind": "run", "planned": TARGET, "actual": actual,
            "hit": delta == 0, "delta_s": delta}


# The 2026-09-30 session as stored after the grouping fix.
ALL_FAST = {"steps_hit": 0, "steps_total": 5, "misses": [], "steps": [
    _step(2, 5.08, -50), _step(4, 4.72, -72), _step(6, 4.49, -86),
    _step(8, 4.6, -79), _step(10, 4.31, -96)]}

INTERVAL_STEPS = [
    {"kind": "warmup", "dist_m": 1500},
    {"kind": "repeat", "reps": 5, "steps": [
        {"kind": "run", "dur_s": 120, "pace_min_km": TARGET},
        {"kind": "recovery", "dur_s": 120}]},
    {"kind": "cooldown", "dist_m": 1500},
]


# ---------- the detector ----------

def test_every_rep_fast_is_a_calibration_signal():
    v = stepmatch.calibration(ALL_FAST)
    assert v["direction"] == "fast" and v["steps"] == 5 and v["missed"] == 5
    assert v["median_delta_s"] == -79
    assert v["planned"] == TARGET and v["actual"][-1] == 4.31


def test_every_rep_slow_is_one_too():
    sm = {"steps": [_step(i, 6.8, 30 + i) for i in (2, 4, 6, 8)]}
    assert stepmatch.calibration(sm)["direction"] == "slow"


def test_a_mixed_session_is_not():
    # two fast, two slow: the reps were uneven, not the targets wrong
    sm = {"steps": [_step(2, 5.0, -50), _step(4, 5.1, -40),
                    _step(6, 6.8, 40), _step(8, 6.9, 45)]}
    assert stepmatch.calibration(sm) is None


def test_small_misses_and_short_sessions_are_not():
    near = {"steps": [_step(i, 5.8, -6) for i in (2, 4, 6, 8)]}
    assert stepmatch.calibration(near) is None                 # lap noise, not a wrong target
    two = {"steps": [_step(2, 4.5, -80), _step(4, 4.4, -90)]}
    assert stepmatch.calibration(two) is None                  # too few reps to call it
    assert stepmatch.calibration(None) is None
    assert stepmatch.calibration({"steps_hit": 3, "steps_total": 4}) is None   # pre-UI-08 row


def test_steps_never_run_do_not_count_as_slow():
    sm = {"steps": [_step(2, 4.6, -80), _step(4, 4.5, -85), _step(6, 4.7, -75),
                    {"step": 8, "kind": "run", "planned": TARGET, "actual": None,
                     "hit": False, "delta_s": None}]}
    v = stepmatch.calibration(sm)
    assert v["direction"] == "fast" and v["steps"] == 3


def test_work_pace_is_the_first_targeted_working_step():
    assert stepmatch.work_pace(INTERVAL_STEPS) == TARGET
    assert stepmatch.work_pace([{"kind": "run", "dist_m": 5000, "hr_zone": 2}]) is None
    assert stepmatch.work_pace(None) is None


# ---------- the hook ----------

_n = iter(range(1, 10_000))


async def _setup(session, *, step_match=ALL_FAST, **user_kw):
    n = next(_n)
    user_kw.setdefault("telegram_chat_id", 5550 + n)
    user_kw.setdefault("plan_adapt_enabled", True)
    user = User(email=f"cal{n}@x.com", password_hash="x", **user_kw)
    session.add(user)
    await session.commit()
    plan = TrainingPlan(user_id=user.id, goal="g", status="active", start_date="2026-07-13")
    session.add(plan)
    await session.flush()
    today = dt.date.today()
    act = ActivityRecord(user_id=user.id, activity_id=7001 + user.id, date=today.isoformat(),
                         type="running", dist_km=6.17, dur_min=42.4, step_match=step_match)
    session.add(act)
    await session.flush()
    session.add(PlannedWorkout(plan_id=plan.id, user_id=user.id, date=today.isoformat(),
                               type="intervals", dist_km=6.2, steps=INTERVAL_STEPS,
                               status="done", completed_activity_id=act.id))
    nxt = (today + dt.timedelta(days=7)).isoformat()
    session.add(PlannedWorkout(plan_id=plan.id, user_id=user.id, date=nxt,
                               type="intervals", dist_km=6.2, steps=INTERVAL_STEPS,
                               status="planned"))
    await session.commit()
    return user, plan, act, nxt


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


def _ctx():
    return SimpleNamespace(bot=_Bot())


def _faster(nxt):
    steps = [PlanStep(kind="warmup", dist_m=1500),
             PlanStep(kind="repeat", reps=5, steps=[
                 PlanStep(kind="run", dur_s=120, pace_min_km=[5.08, 5.42]),
                 PlanStep(kind="recovery", dur_s=120)]),
             PlanStep(kind="cooldown", dist_m=1500)]
    return PlanEdit(summary="усі 5 відрізків на ~1:20/км швидші за ціль — піднімаю темп",
                    operations=[PlanOp(action="modify", date=nxt, steps=steps)], risky=False)


CREDS = SimpleNamespace(anthropic_key="k")


async def test_a_systematic_miss_sends_one_concrete_proposal(session):
    user, plan, act, nxt = await _setup(session)
    ctx = _ctx()
    with patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(plan, _faster(nxt)))) as m:
        await jobs_module._calibration_check(ctx, session, user, CREDS, act)
        # the next run in the same streak must not ask (or pay) again
        await jobs_module._calibration_check(ctx, session, user, CREDS, act)
    assert m.await_count == 1
    kw = m.await_args.kwargs
    assert kw["trigger"] == "calibration"
    assert kw["calibration"]["direction"] == "fast" and kw["calibration"]["type"] == "intervals"
    assert len(ctx.bot.sent) == 1
    text = ctx.bot.sent[0][1]
    # what changes, in numbers — not "деталі сесії"
    assert "темп 5:55–6:15 → 5:05–5:25/км" in text
    assert await repository.get_state(session, user.id, jobs_module.PENDING_ADAPT_KEY)


async def test_no_signal_no_call(session):
    near = {"steps": [_step(i, 5.8, -6) for i in (2, 4, 6, 8)]}
    user, plan, act, _ = await _setup(session, step_match=near)
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        await jobs_module._calibration_check(_ctx(), session, user, CREDS, act)
    m.assert_not_called()


async def test_adaptation_off_or_a_pending_proposal_means_no_call(session):
    user, plan, act, _ = await _setup(session, plan_adapt_enabled=False)
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        await jobs_module._calibration_check(_ctx(), session, user, CREDS, act)
    m.assert_not_called()

    user2, plan2, act2, _ = await _setup(session)
    await repository.set_state(session, user2.id, jobs_module.PENDING_ADAPT_KEY, "{}")
    await session.commit()
    with patch.object(jobs_module, "run_plan_adaptation", new=AsyncMock()) as m:
        await jobs_module._calibration_check(_ctx(), session, user2, CREDS, act2)
    m.assert_not_called()


async def test_an_all_fine_answer_sends_nothing_but_still_burns_the_guard(session):
    user, plan, act, _ = await _setup(session)
    ctx = _ctx()
    fine = PlanEdit(summary="ок", operations=[], risky=False)
    with patch.object(jobs_module, "run_plan_adaptation",
                      new=AsyncMock(return_value=(plan, fine))) as m:
        await jobs_module._calibration_check(ctx, session, user, CREDS, act)
        await jobs_module._calibration_check(ctx, session, user, CREDS, act)
    assert m.await_count == 1 and ctx.bot.sent == []


async def test_the_adaptation_context_carries_the_verdict_and_the_steps(session, monkeypatch):
    """The model can only re-set a target it was shown, and only keep a structure it was
    shown — upcoming structured sessions go in with their steps."""
    user, plan, act, nxt = await _setup(session)
    captured = {}

    def fake(context, api_key=None):
        captured.update(context)
        return (PlanEdit(summary="ок", operations=[], risky=False),
                SimpleNamespace(kind="adapt", model="m", input_tokens=0, output_tokens=0,
                                cost_usd=0.0, cached=False))

    monkeypatch.setattr(plans_mod, "plan_adapt_with_stats", fake)
    verdict = {**stepmatch.calibration(ALL_FAST), "date": act.date, "type": "intervals"}
    await plans_mod.run_plan_adaptation(session, user_id=user.id, api_key="k",
                                        trigger="calibration", calibration=verdict)
    assert captured["trigger"] == "calibration"
    assert captured["calibration"]["direction"] == "fast"
    up = {u["date"]: u for u in captured["upcoming"]}
    assert up[nxt]["steps"] == INTERVAL_STEPS
