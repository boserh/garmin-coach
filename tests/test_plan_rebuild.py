"""A weekly-schedule change from chat («3 пробіжки на тиждень замість 2») rebuilds the rest
of the plan instead of sprinkling sessions into it — and writes the schedule onto the plan.

Covers the pure rules (``app.planschedule``), the rebuild itself
(``analysis.plans.run_plan_rebuild``: what is kept, what is replaced, what is written,
what happens when it fails), and both front-ends' confirm paths. Every Claude call and
every Garmin call is mocked — the suite spends $0.
"""
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

from app import planschedule
from app.analysis import plans
from app.analysis.client import AnalystError, CallStats
from app.core.crypto import hash_password
from app.db import users
from app.db.base import async_session_maker
from app.db.models import PlannedWorkout, ReportLog, TrainingPlan, User
from app.garmin import repository
from app.garmin.schemas import GeneratedPlan, PlanEdit, PlanOp, PlanWorkout, ScheduleOp
from tests.web_helpers import wait_live_jobs

U1 = 1
TODAY = dt.date.today()


def _iso(delta_days: int) -> str:
    return (TODAY + dt.timedelta(days=delta_days)).isoformat()


def _slug(delta_days: int) -> str:
    return planschedule.WEEKDAYS[(TODAY + dt.timedelta(days=delta_days)).weekday()]


# ---------- pure rules ----------

def test_normalize_orders_dedups_and_keeps_the_long_run_day():
    cur = {"run_days": ["tue", "sun"], "long_run_day": "sun", "strength_days": []}
    new = planschedule.normalize(
        {"run_days": ["sun", "Thu", "tue", "tue", "xyz"], "long_run_day": None}, cur)
    assert new == {"run_days": ["tue", "thu", "sun"], "long_run_day": "sun",
                   "strength_days": None}


def test_normalize_moves_a_long_run_day_that_is_no_longer_a_run_day():
    cur = {"run_days": ["tue", "sun"], "long_run_day": "sun", "strength_days": []}
    new = planschedule.normalize({"run_days": ["mon", "wed", "sat"],
                                  "long_run_day": "sun"}, cur)
    assert new["long_run_day"] == "sat"      # the last run day, like the setup form


def test_normalize_refuses_fewer_than_two_run_days():
    with pytest.raises(planschedule.ScheduleError):
        planschedule.normalize({"run_days": ["sun"]}, {"run_days": ["tue", "sun"]})


def test_normalize_ignores_strength_days_on_a_plan_without_strength():
    """There is no session to put on a new strength day — inventing one is the setup
    form's job, not a schedule change's."""
    cur = {"run_days": ["tue", "sun"], "long_run_day": "sun", "strength_days": []}
    new = planschedule.normalize({"run_days": ["tue", "sun"], "strength_days": ["sat"]}, cur)
    assert new["strength_days"] is None
    assert not planschedule.is_change(new, cur | {"long_run_day": "sun"})


def test_normalize_unchanged_strength_days_is_no_strength_change():
    cur = {"run_days": ["tue", "sun"], "long_run_day": "sun", "strength_days": ["mon", "thu"]}
    new = planschedule.normalize({"run_days": ["tue", "thu", "sun"],
                                  "strength_days": ["thu", "mon"]}, cur)
    assert new["strength_days"] is None


def test_remap_cycles_and_drops_in_weekday_order():
    assert planschedule.remap(["mon", "thu"], ["sat"]) == {"sat": "mon"}
    assert planschedule.remap(["mon", "thu"], ["mon", "wed", "fri"]) == \
        {"mon": "mon", "wed": "thu", "fri": "mon"}
    assert planschedule.remap(["mon", "thu"], ["thu"]) == {"thu": "thu"}   # keeps its own
    assert planschedule.remap(["mon", "thu"], ["thu", "sat"]) == {"thu": "thu", "sat": "mon"}
    assert planschedule.remap([], ["mon"]) == {}


def test_remap_strength_intake_moves_every_weekday_keyed_piece():
    strength = {"enabled": True, "assignments": {"mon": 11}, "custom": {"thu": "ноги"},
                "custom_generated": {"thu": {"name": "Ноги"}}}
    out = planschedule.remap_strength_intake(strength, ["tue", "sat"])
    assert out == {"enabled": True, "assignments": {"tue": 11}, "custom": {"sat": "ноги"},
                   "custom_generated": {"sat": {"name": "Ноги"}}}
    assert planschedule.remap_strength_intake(strength, []) == {"enabled": False}


def test_describe_and_confirmation_lines():
    cur = {"run_days": ["tue", "sun"], "long_run_day": "sun", "strength_days": ["mon", "thu"]}
    new = {"run_days": ["tue", "thu", "sun"], "long_run_day": "sun", "strength_days": ["mon"]}
    lines = planschedule.describe(new, cur)
    assert lines == ["біг: 2 → 3 дні на тиждень (вт, чт, нд; довгий — нд)",
                     "силові: 2 → 1 (пн)"]
    full = planschedule.confirmation_lines(
        dict(new, lines=lines, start="2026-10-02", cost_usd=0.5))
    assert full[:2] == lines
    assert full[2].startswith("з 02.10: решту програми буде згенеровано заново")
    assert full[3] == "генерація: 1–2 хв, до ~$0.50"
    assert planschedule.describe({"run_days": ["mon", "tue", "wed", "thu", "fri"],
                                  "long_run_day": "fri", "strength_days": None},
                                 {"run_days": [], "days_per_week": 3})[0] \
        .startswith("біг: 3 → 5 днів на тиждень")


# ---------- the rebuild ----------

async def _seed(session, *, strength=False, target_date=None, pushed=False):
    """A plan on tue/sun, a past done + missed session, today's session, and a future
    tail of runs (plus strength on mon/thu when asked)."""
    intake = {"run_days": ["tue", "sun"], "long_run_day": "sun", "adjust_level": "flexible"}
    if strength:
        intake["strength"] = {"enabled": True, "assignments": {_slug(1): 77},
                              "custom": {_slug(4): "ноги"}}
    plan = TrainingPlan(user_id=U1, goal="general" if not target_date else "first_10k",
                        status="active", start_date=_iso(-21), target_date=target_date,
                        days_per_week=2, intensity="moderate", intake=intake,
                        summary="старий підхід")
    session.add(plan)
    await session.flush()

    def add(delta, type_="easy", status="planned", **kw):
        kw.setdefault("description", f"{type_} {delta}")
        w = PlannedWorkout(plan_id=plan.id, user_id=U1, date=_iso(delta), week=1,
                           type=type_, status=status, **kw)
        session.add(w)
        return w

    add(-5, "long", "done", dist_km=10.0)
    add(-3, "easy", "missed", dist_km=5.0)
    add(0, "easy", dist_km=5.0)                                       # today stays
    future = [add(d, "easy", dist_km=5.0) for d in (2, 5, 9, 12)]
    if pushed:
        future[0].garmin_workout_id, future[0].garmin_schedule_id = 900, 901
    if strength:
        add(1, "strength", garmin_template_id=77, description="Day 1",
            strength_snapshot={"name": "Day 1", "exercises": []})
        add(4, "strength", strength_plan={"name": "Ноги", "blocks": []})
    await session.commit()
    return plan


def _gen(*deltas, type_="easy"):
    return GeneratedPlan(summary="новий підхід на 3 дні", workouts=[
        PlanWorkout(date=_iso(d), week=1, type=type_, dist_km=6.0, description=f"нова {d}")
        for d in deltas])


def _patch_gen(out=None, *, raises=None, seen=None):
    def fake(context, api_key=None, model=None):
        if seen is not None:
            seen.update(context)
        if raises:
            raise raises
        return out, CallStats(kind="plan", model="claude-opus-4-8", input_tokens=10,
                              output_tokens=20, cost_usd=0.4)
    return patch.object(plans, "generate_plan_with_stats", side_effect=fake)


NEW = {"run_days": ["tue", "thu", "sun"], "long_run_day": "sun"}


async def test_rebuild_replaces_only_the_planned_tail_and_writes_the_schedule(session):
    plan = await _seed(session)
    seen = {}
    with _patch_gen(_gen(1, 3, 6), seen=seen):
        res = await plans.run_plan_rebuild(session, user_id=U1, schedule=NEW)

    ws = await repository.list_workouts(session, plan.id)
    by_date = {w.date: w for w in ws}
    # history and today untouched …
    assert by_date[_iso(-5)].status == "done" and by_date[_iso(-3)].status == "missed"
    assert by_date[_iso(0)].description == "easy 0"
    # … the old tail is gone, the new one is in, in the SAME plan
    assert not any(w.description.startswith("easy ") and w.date > _iso(0) for w in ws)
    assert {w.date for w in ws if w.date > _iso(0)} == {_iso(1), _iso(3), _iso(6)}
    assert res["added"] == 3 and res["removed"] == 4
    assert len(await repository.list_plans(session, U1, status="active")) == 1
    # the schedule now lives on the plan, where extension and /ask read it
    assert plan.days_per_week == 3
    assert plan.intake["run_days"] == ["tue", "thu", "sun"]
    assert plan.intake["adjust_level"] == "flexible"          # the rest of intake kept
    assert plan.summary == "новий підхід на 3 дні"
    # week numbers continue the plan's own count (it started three weeks ago)
    assert by_date[_iso(3)].week == (TODAY + dt.timedelta(days=3) -
                                     dt.date.fromisoformat(_iso(-21))).days // 7 + 1
    # the model was told it's a rebuild, on the NEW days, with the real history
    assert seen["rebuild"] is True and seen["run_days"] == ["tue", "thu", "sun"]
    assert seen["days_per_week"] == 3 and seen["start_date"] == _iso(1)
    assert [h["status"] for h in seen["previous_weeks"]] == ["done", "missed", "planned"]


async def test_rebuild_keeps_strength_when_its_days_dont_change(session):
    plan = await _seed(session, strength=True)
    with _patch_gen(_gen(2, 3, 6)):
        await plans.run_plan_rebuild(session, user_id=U1, schedule=NEW)
    strength = [w for w in await repository.list_workouts(session, plan.id)
                if w.type == "strength"]
    assert {w.date for w in strength} == {_iso(1), _iso(4)}   # the very same rows


async def test_rebuild_relays_strength_onto_new_days_without_claude_or_garmin(session):
    """«заміни одну силову на біг»: strength days change too. The sessions come from the
    plan's own rows — no strength generation call, no Garmin template fetch."""
    plan = await _seed(session, strength=True)
    keep_day = _slug(1)                     # the template day (Day 1)
    schedule = dict(NEW, strength_days=[keep_day])
    with _patch_gen(_gen(2, 3, 6)), \
            patch.object(plans, "generate_strength_with_stats") as s1, \
            patch.object(plans, "generate_strength_progression_with_stats") as s2, \
            patch("app.garmin.client.fetch_workouts") as fw:
        await plans.run_plan_rebuild(session, user_id=U1, schedule=schedule)
    s1.assert_not_called()
    s2.assert_not_called()
    fw.assert_not_called()
    strength = [w for w in await repository.list_workouts(session, plan.id)
                if w.type == "strength" and w.date > _iso(0)]
    assert strength and all(planschedule.WEEKDAYS[dt.date.fromisoformat(w.date).weekday()]
                            == keep_day for w in strength)
    assert all(w.garmin_template_id == 77 for w in strength)
    # the intake follows, so a later extension lays strength on the new day too
    assert plan.intake["strength"]["assignments"] == {keep_day: 77}


async def test_a_failed_generation_changes_nothing(session):
    plan = await _seed(session)
    before = [(w.date, w.description) for w in await repository.list_workouts(session, plan.id)]
    with _patch_gen(raises=AnalystError("перевантажено")):
        with pytest.raises(AnalystError):
            await plans.run_plan_rebuild(session, user_id=U1, schedule=NEW)
    after = [(w.date, w.description) for w in await repository.list_workouts(session, plan.id)]
    assert after == before
    assert plan.days_per_week == 2 and plan.intake["run_days"] == ["tue", "sun"]


async def test_an_empty_generation_changes_nothing_but_is_logged(session):
    """Sessions outside the window or of the strength type don't count — a reply with
    nothing else is a failure, not a plan with no future."""
    plan = await _seed(session)
    out = GeneratedPlan(summary="?", workouts=[
        PlanWorkout(date=_iso(-1), week=1, type="easy", dist_km=5.0, description="мин"),
        PlanWorkout(date=_iso(3), week=1, type="strength", description="силова")])
    with _patch_gen(out):
        with pytest.raises(AnalystError):
            await plans.run_plan_rebuild(session, user_id=U1, schedule=NEW)
    assert len([w for w in await repository.list_workouts(session, plan.id)
                if w.date > _iso(0)]) == 4
    row = (await session.execute(
        ReportLog.__table__.select().where(ReportLog.kind == "plan"))).first()
    assert row is not None and row.ok is False      # the money spent is still on record


async def test_rebuild_refuses_before_paying_when_pushed_sessions_cant_be_removed(session):
    await _seed(session, pushed=True)
    provider = SimpleNamespace(login=lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with patch("app.garmin.providers.get_provider", return_value=provider), \
            _patch_gen(_gen(1)) as gen:
        with pytest.raises(AnalystError, match="Garmin"):
            await plans.run_plan_rebuild(session, user_id=U1, schedule=NEW)
    gen.assert_not_called()


async def test_rebuild_takes_pushed_sessions_off_the_calendar_first(session):
    plan = await _seed(session, pushed=True)
    provider = SimpleNamespace(login=lambda: None)
    removed = []

    async def fake_remove(_session, w):
        removed.append(w.garmin_workout_id)

    with patch("app.garmin.providers.get_provider", return_value=provider), \
            patch("app.garmin.plan_sync.remove_workout", side_effect=fake_remove), \
            _patch_gen(_gen(1, 3)):
        await plans.run_plan_rebuild(session, user_id=U1, schedule=NEW)
    assert removed == [900]
    assert len([w for w in await repository.list_workouts(session, plan.id)
                if w.date > _iso(0)]) == 2


async def test_race_plan_rebuilds_up_to_the_race(session):
    await _seed(session, target_date=_iso(30))
    seen = {}
    with _patch_gen(_gen(1, 3, 30, 31), seen=seen):
        res = await plans.run_plan_rebuild(session, user_id=U1, schedule=NEW)
    assert seen["target_date"] == _iso(30) and seen["open_ended"] is False
    assert res["added"] == 3                 # the session past the race is dropped


# ---------- the proposal ----------

async def test_schedule_proposal(session):
    plan = await _seed(session)
    assert plans.schedule_proposal(plan, None) is None
    assert plans.schedule_proposal(
        plan, ScheduleOp(run_days=["tue", "sun"], long_run_day="sun")) is None   # no change
    prop = plans.schedule_proposal(plan, ScheduleOp(run_days=["sun", "tue", "thu"]))
    assert prop["run_days"] == ["tue", "thu", "sun"] and prop["start"] == _iso(1)
    assert prop["lines"][0].startswith("біг: 2 → 3 дні")
    assert 0 < prop["cost_usd"] < 2.0          # below the per-call ceiling it must pass
    with pytest.raises(AnalystError):
        plans.schedule_proposal(plan, ScheduleOp(run_days=["sun"]))


async def test_run_plan_edit_shows_the_model_the_current_schedule(session):
    await _seed(session)
    seen = {}

    def fake(context, api_key=None):
        seen.update(context)
        return PlanEdit(summary="ок", operations=[]), CallStats(kind="plan_edit", model="m")

    with patch.object(plans, "plan_edit_with_stats", fake):
        await plans.run_plan_edit(session, user_id=U1, instruction="3 пробіжки замість 2")
    assert seen["schedule"]["run_days"] == ["tue", "sun"]
    assert seen["schedule"]["days_per_week"] == 2


# ---------- bot ----------

class _FakeMessage:
    def __init__(self, text="", chat_id=555, message_id=1):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.chat_id = chat_id
        self.message_id = message_id
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))
        return _FakeMessage(text, self.chat_id, 100 + len(self.replies))


class _FakeQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.texts = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.texts.append(text)


@pytest.fixture
def bot_env(session, monkeypatch):
    from bot import handlers as h

    @asynccontextmanager
    async def maker():
        yield session

    @asynccontextmanager
    async def runtime(_session, _user):
        yield SimpleNamespace(anthropic_key="k")

    monkeypatch.setattr(h, "async_session_maker", maker)
    monkeypatch.setattr(h, "user_runtime", runtime)
    return h


async def _mk_user(session, chat_id=555):
    u = User(email=f"{chat_id}@e.com", password_hash="h", is_approved=True,
             is_active=True, telegram_chat_id=chat_id, garmin_sync_enabled=False)
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


async def test_bot_schedule_proposal_offers_a_rebuild(bot_env, session):
    h = bot_env
    user = await _mk_user(session)
    plan = await _seed(session)
    edit = PlanEdit(summary="Третій біг у четвер.", schedule=ScheduleOp(**NEW),
                    operations=[PlanOp(action="add", date=_iso(3), type="easy")])
    msg = _FakeMessage("3 пробіжки замість 2")
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=555))
    with patch.object(h, "run_plan_edit", AsyncMock(return_value=(plan, edit))):
        await h._plan_edit(update, SimpleNamespace(bot=None), "3 пробіжки замість 2")

    text, kb = msg.replies[-1]
    assert "перебудувати план" in text and "біг: 2 → 3 дні" in text and "до ~$" in text
    assert kb.inline_keyboard[0][0].text == "✅ Перебудувати"
    pending = await repository.get_pending_plan_edit(session, user.id)
    assert pending["schedule"]["run_days"] == ["tue", "thu", "sun"]
    assert pending["ops"] == []              # a rebuild, not operations


async def test_bot_confirm_runs_the_rebuild(bot_env, session):
    h = bot_env
    user = await _mk_user(session, chat_id=556)
    await _seed(session)
    schedule = plans.schedule_proposal(
        await repository.get_active_plan(session, U1), ScheduleOp(**NEW))
    await repository.set_pending_plan_edit(session, user.id, [], [], summary="s",
                                           schedule=schedule)
    upd = SimpleNamespace(callback_query=_FakeQuery("plan_apply", 556))
    fake = AsyncMock(return_value={"added": 9, "summary": "новий підхід"})
    with patch.object(h, "run_plan_rebuild", fake):
        await h.plan_callback(upd, None)
    fake.assert_awaited_once()
    assert fake.await_args.kwargs["schedule"]["run_days"] == ["tue", "thu", "sun"]
    assert fake.await_args.kwargs["api_key"] == "k"
    assert upd.callback_query.texts[-1].startswith("✅ План перебудовано")
    assert await repository.get_pending_plan_edit(session, user.id) is None


async def test_bot_cancel_never_rebuilds(bot_env, session):
    h = bot_env
    user = await _mk_user(session, chat_id=557)
    await repository.set_pending_plan_edit(session, user.id, [], [], schedule=NEW)
    upd = SimpleNamespace(callback_query=_FakeQuery("plan_cancel", 557))
    with patch.object(h, "run_plan_rebuild", AsyncMock()) as fake:
        await h.plan_callback(upd, None)
    fake.assert_not_called()


async def test_bot_rebuild_failure_is_reported(bot_env, session):
    h = bot_env
    user = await _mk_user(session, chat_id=558)
    await repository.set_pending_plan_edit(session, user.id, [], [], schedule=NEW)
    upd = SimpleNamespace(callback_query=_FakeQuery("plan_apply", 558))
    with patch.object(h, "run_plan_rebuild", AsyncMock(side_effect=AnalystError("ой"))):
        await h.plan_callback(upd, None)
    assert upd.callback_query.texts[-1] == "Не вдалось перебудувати план: ой"


# ---------- web ----------

@pytest.fixture
def web(request):
    from app.main import create_app
    from app.routers import chat as chat_router

    email = f"{request.node.name}@example.com"

    async def seed():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, email) or await users.create_user(
                s, email=email, password_hash=hash_password("pw"), is_admin=False)
            plan = TrainingPlan(user_id=u.id, goal="general", status="active",
                                start_date=_iso(-7), days_per_week=2,
                                intake={"run_days": ["tue", "sun"], "long_run_day": "sun"})
            s.add(plan)
            await s.commit()
            return u.id

    with TestClient(create_app()) as c:     # the lifespan creates the tables
        uid = anyio.run(seed)
        assert c.post("/login", data={"email": email, "password": "pw"}).status_code == 200
        yield c, uid, chat_router


def _pending(uid):
    async def read():
        async with async_session_maker() as s:
            return await repository.get_pending_plan_edit(s, uid)
    return anyio.run(read)


def _plan(uid):
    async def read():
        async with async_session_maker() as s:
            return await repository.get_active_plan(s, uid)
    return anyio.run(read)


def test_web_schedule_proposal_is_stored_and_shown(web):
    client, uid, chat_router = web
    edit = PlanEdit(summary="Третій біг у четвер.", schedule=ScheduleOp(**NEW), operations=[])
    with patch.object(chat_router, "run_plan_edit",
                      AsyncMock(return_value=(_plan(uid), edit))):
        client.post("/chat", data={"message": "три пробіжки замість двох"})
        wait_live_jobs()
    assert _pending(uid)["schedule"]["run_days"] == ["tue", "thu", "sun"]
    page = client.get("/chat").text
    assert "біг: 2 → 3 дні" in page and "Перебудувати" in page


def test_web_confirm_starts_the_rebuild_and_goes_to_the_plan(web):
    client, uid, chat_router = web

    async def stage():
        async with async_session_maker() as s:
            await repository.set_pending_plan_edit(s, uid, [], [], summary="s", schedule=NEW)
    anyio.run(stage)
    with patch.object(chat_router.plan_router, "spawn_plan_rebuild",
                      AsyncMock(return_value=True)) as spawn:
        r = client.post("/chat/confirm", data={"action": "apply"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/plan"
    assert spawn.await_args.args[1:] == (uid, NEW)
    assert _pending(uid) is None


def test_web_confirm_while_generating_keeps_the_proposal(web):
    client, uid, chat_router = web

    async def stage():
        async with async_session_maker() as s:
            await repository.set_pending_plan_edit(s, uid, [], [], summary="s", schedule=NEW)
    anyio.run(stage)
    with patch.object(chat_router.plan_router, "generation_running",
                      AsyncMock(return_value=True)), \
            patch.object(chat_router.plan_router, "spawn_plan_rebuild", AsyncMock()) as spawn:
        r = client.post("/chat/confirm", data={"action": "apply"}, follow_redirects=False)
    assert r.headers["location"].startswith("/chat?err=")
    spawn.assert_not_called()
    assert _pending(uid)["schedule"] == NEW


def test_web_background_rebuild_reports_its_result_on_the_plan_page(web):
    client, uid, _chat = web
    from app.routers import plan as plan_router

    @asynccontextmanager
    async def runtime(_session, _user):
        yield SimpleNamespace(anthropic_key="k")

    with patch.object(plan_router, "user_runtime", runtime), \
            patch.object(plan_router, "run_plan_rebuild",
                         AsyncMock(return_value={"added": 7})):
        anyio.run(plan_router._rebuild_plan_bg, uid, NEW)
    page = client.get("/plan").text
    assert "План перебудовано під новий розклад — 7 нових тренувань" in page
    assert "План перебудовано" not in client.get("/plan").text    # shown once

    with patch.object(plan_router, "user_runtime", runtime), \
            patch.object(plan_router, "run_plan_rebuild",
                         AsyncMock(side_effect=AnalystError("Garmin зайнятий"))):
        anyio.run(plan_router._rebuild_plan_bg, uid, NEW)
    assert "Не вдалось перебудувати план: Garmin зайнятий" in client.get("/plan").text
