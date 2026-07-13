"""Open-ended (`general`) plans: generation flags, rolling extension, auto-extend job.

All Claude calls are mocked — the suite spends $0.
"""
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.analysis import plans
from app.analysis.service import CallStats, run_plan_extension, run_plan_generation
from app.db.models import PlannedWorkout, TrainingPlan, User
from app.garmin import repository
from app.garmin.schemas import GeneratedPlan, PlanWorkout
from bot import jobs as jobs_module

U1 = 1


def _gen(summary="підхід", workouts=None):
    return GeneratedPlan(
        summary=summary,
        workouts=workouts if workouts is not None else [
            PlanWorkout(date="2026-07-15", week=1, type="easy", dist_km=4.0, description="легко"),
            PlanWorkout(date="2026-07-17", week=1, type="long", dist_km=8.0, description="довгий"),
        ],
    )


# ---------- generation: open-ended goal ----------

async def test_general_goal_is_open_ended(session):
    captured = {}

    def fake_gen(context, api_key=None, model=None):
        captured["ctx"] = context
        return _gen(), CallStats(kind="plan", model="m")

    with patch.object(plans, "generate_plan_with_stats", side_effect=fake_gen):
        plan = await run_plan_generation(
            session, user_id=U1, goal="general", goal_label="Регулярний біг",
            target_date="2026-12-01",  # should be ignored for an open-ended goal
            start_date="2026-07-13", days_per_week=3, intensity="moderate",
            intake={"run_days": ["mon", "wed", "fri"], "long_run_day": "fri"}, api_key=None)

    # The stored plan is never pinned to a race date …
    assert plan.goal == "general" and plan.target_date is None
    # … but the model gets a concrete block end + the open_ended flag (no taper).
    assert captured["ctx"]["open_ended"] is True
    # 6-week block from 2026-07-13 → last day 2026-08-23 (start + 6*7 - 1).
    assert captured["ctx"]["target_date"] == "2026-08-23"


async def test_race_goal_keeps_target_date(session):
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(_gen(), CallStats(kind="plan", model="m"))):
        plan = await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="Перші 5 км",
            target_date="2026-08-01", start_date="2026-07-13", days_per_week=3,
            intensity="moderate", intake={}, api_key=None)
    assert plan.target_date == "2026-08-01"


# ---------- repository helpers ----------

async def _seed_general_plan(session, *, target_date=None, goal="general", n_weeks=6):
    plan = TrainingPlan(
        user_id=U1, goal=goal, goal_label="lbl", status="active",
        start_date="2026-07-13", target_date=target_date, days_per_week=3,
        intensity="moderate", intake={"run_days": ["mon", "wed", "fri"], "long_run_day": "fri"},
    )
    session.add(plan)
    await session.flush()
    base = dt.date(2026, 7, 13)
    for wk in range(n_weeks):
        d = (base + dt.timedelta(weeks=wk)).isoformat()
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=U1, date=d, week=wk + 1,
            type="easy", dist_km=4.0, description="d", status="planned"))
    await session.commit()
    return plan


async def test_last_workout_date_and_append(session):
    plan = await _seed_general_plan(session, n_weeks=3)
    last = await repository.last_workout_date(session, plan.id)
    assert last == (dt.date(2026, 7, 13) + dt.timedelta(weeks=2)).isoformat()

    new = [PlanWorkout(date="2026-08-10", week=1, type="tempo", dist_km=6.0, description="t")]
    added = await repository.append_workouts(session, plan, new, week_offset=3)
    assert added == 1
    ws = await repository.list_workouts(session, plan.id)
    assert len(ws) == 4
    appended = next(w for w in ws if w.date == "2026-08-10")
    assert appended.week == 4  # 1 + week_offset(3)


# ---------- extension ----------

async def test_run_plan_extension_appends_without_archiving(session):
    plan = await _seed_general_plan(session, n_weeks=6)
    new_ws = [
        PlanWorkout(date="2026-08-25", week=1, type="easy", dist_km=5.0, description="легко"),
        PlanWorkout(date="2026-08-27", week=2, type="long", dist_km=9.0, description="довгий"),
    ]
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(_gen(workouts=new_ws), CallStats(kind="plan", model="m"))):
        out = await run_plan_extension(session, user_id=U1, api_key=None)

    assert out is not None and out.id == plan.id and out.status == "active"
    ws = await repository.list_workouts(session, plan.id)
    assert len(ws) == 8  # 6 seeded + 2 appended
    ext = next(w for w in ws if w.date == "2026-08-27")
    assert ext.week == 8  # week 2 continued past the 6-week block (offset 6)
    # still exactly one active plan (never archived/regenerated)
    plans_all = await repository.list_plans(session, U1, status="active")
    assert len(plans_all) == 1


async def test_extension_noop_for_race_plan(session):
    await _seed_general_plan(session, goal="first_5k", target_date="2026-09-01", n_weeks=6)
    with patch.object(plans, "generate_plan_with_stats") as m:
        out = await run_plan_extension(session, user_id=U1, api_key=None)
    assert out is None
    m.assert_not_called()   # no Claude call when there's nothing to extend


# ---------- morning extend nudge (confirm-only) ----------

from bot import handlers as handlers_module  # noqa: E402

TODAY = dt.date.today().isoformat()


async def _make_user(session, **kw):
    kw.setdefault("telegram_chat_id", 555)
    kw.setdefault("is_active", True)
    kw.setdefault("is_approved", True)
    kw.setdefault("garmin_sync_enabled", False)  # keep Garmin out of the test
    user = User(email=f"u{id(kw)}@x.com", password_hash="x", **kw)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


class _FakeCtx:
    def __init__(self):
        self.sent = []
        self.bot = SimpleNamespace(send_message=self._send)

    async def _send(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))


@asynccontextmanager
async def _fake_runtime(session_, user_):
    yield SimpleNamespace(anthropic_key="k", has_garmin=False)


async def _seed_for_user(session, user_id, *, last_offset_days, goal="general", target_date=None):
    plan = TrainingPlan(
        user_id=user_id, goal=goal, status="active",
        start_date="2026-01-01", target_date=target_date, days_per_week=3, intensity="moderate",
    )
    session.add(plan)
    await session.flush()
    last = (dt.date.today() + dt.timedelta(days=last_offset_days)).isoformat()
    session.add(PlannedWorkout(
        plan_id=plan.id, user_id=user_id, date=last, week=6,
        type="easy", dist_km=4.0, description="d", status="planned"))
    await session.commit()
    return plan


async def test_nudge_skips_when_runway_remains(session):
    user = await _make_user(session)
    await _seed_for_user(session, user.id, last_offset_days=30)  # far from the end
    ctx = _FakeCtx()
    await jobs_module._extend_nudge_for_user(ctx, session, user, TODAY)
    assert ctx.sent == []


async def test_nudge_fires_with_buttons_when_near_end(session):
    user = await _make_user(session)
    await _seed_for_user(session, user.id, last_offset_days=5)  # within lead window
    ctx = _FakeCtx()
    await jobs_module._extend_nudge_for_user(ctx, session, user, TODAY)
    assert len(ctx.sent) == 1 and ctx.sent[0][0] == 555
    assert ctx.sent[0][2] is not None   # inline ✅/❌ keyboard
    # once/day guard set → a re-tick doesn't re-ask
    ctx2 = _FakeCtx()
    await jobs_module._extend_nudge_for_user(ctx2, session, user, TODAY)
    assert ctx2.sent == []


async def test_nudge_respects_snooze(session):
    user = await _make_user(session)
    await _seed_for_user(session, user.id, last_offset_days=5)
    tomorrow = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    await repository.set_state(session, user.id, handlers_module.PLAN_EXTEND_SNOOZE_KEY, tomorrow)
    ctx = _FakeCtx()
    await jobs_module._extend_nudge_for_user(ctx, session, user, TODAY)
    assert ctx.sent == []


async def test_nudge_ignores_race_plan(session):
    user = await _make_user(session)
    await _seed_for_user(session, user.id, last_offset_days=2,
                         goal="first_5k", target_date="2026-09-01")
    ctx = _FakeCtx()
    await jobs_module._extend_nudge_for_user(ctx, session, user, TODAY)
    assert ctx.sent == []


# ---------- extend confirm callback (generation on ✅) ----------

class _FakeQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.texts = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.texts.append(text)


def _update(data, chat_id=555):
    return SimpleNamespace(callback_query=_FakeQuery(data, chat_id))


def _use_session(session):
    """Make the handler's ``async_session_maker()`` reuse the test's in-memory session
    (the handler opens its own — otherwise it hits the empty file DB)."""
    @asynccontextmanager
    async def _cm():
        yield session

    return patch.object(handlers_module, "async_session_maker", _cm)


async def test_callback_no_sets_snooze(session):
    user = await _make_user(session)
    await _seed_for_user(session, user.id, last_offset_days=5)
    upd = _update("planext:no")
    with _use_session(session), \
            patch.object(handlers_module, "run_plan_extension", new=AsyncMock()) as m:
        await handlers_module.plan_extend_callback(upd, None)
    m.assert_not_called()
    snooze = await repository.get_state(session, user.id, handlers_module.PLAN_EXTEND_SNOOZE_KEY)
    assert snooze and snooze > TODAY


async def test_callback_yes_generates(session):
    user = await _make_user(session)
    await _seed_for_user(session, user.id, last_offset_days=5)
    upd = _update("planext:yes")
    with _use_session(session), \
            patch.object(handlers_module, "run_plan_extension",
                         new=AsyncMock(return_value=SimpleNamespace(id=1))) as m, \
            patch.object(handlers_module, "user_runtime", new=_fake_runtime):
        await handlers_module.plan_extend_callback(upd, None)
    m.assert_awaited_once()
    assert "Додав наступні тижні" in upd.callback_query.texts[-1]


async def test_callback_yes_noop_when_not_near_end(session):
    user = await _make_user(session)
    await _seed_for_user(session, user.id, last_offset_days=40)  # plenty of runway
    upd = _update("planext:yes")
    with _use_session(session), \
            patch.object(handlers_module, "run_plan_extension", new=AsyncMock()) as m, \
            patch.object(handlers_module, "user_runtime", new=_fake_runtime):
        await handlers_module.plan_extend_callback(upd, None)
    m.assert_not_called()   # stale button → no paid generation
    assert "актуально" in upd.callback_query.texts[-1]
