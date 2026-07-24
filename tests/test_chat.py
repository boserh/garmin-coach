"""EP-11: web chat — routing heuristic, GET/POST /chat, and the shared DB-backed
plan-edit confirm state (repository.set_pending_plan_edit / pop_pending_plan_edit)."""
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

from app.core.crypto import hash_password
from app.db import users
from app.db.base import async_session_maker
from app.db.models import PlannedWorkout, TrainingPlan
from app.garmin import repository
from app.garmin.schemas import PlanEdit, PlanOp
from app.main import create_app
from app.routers import chat as chat_router


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def _seed_user(email, password="pw", garmin_sync_enabled=False):
    async def seed():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, email)
            if not u:
                u = await users.create_user(
                    s, email=email, password_hash=hash_password(password), is_admin=False,
                )
            u.garmin_sync_enabled = garmin_sync_enabled
            await s.commit()
            return u.id

    return anyio.run(seed)


@pytest.fixture
def auth_client(client, request):
    # A distinct email per test → a distinct user row → no pending-state/plan bleed
    # between tests sharing the file-backed test DB (unlike the in-memory `session`
    # fixture, this one persists for the whole test run).
    email = f"{request.node.name}@example.com"
    uid = _seed_user(email)
    r = client.post("/login", data={"email": email, "password": "pw"})
    assert r.status_code == 200
    return client, uid


# ---------- routing heuristic ----------

@pytest.mark.parametrize("text", [
    "перенеси довгу на суботу",
    "додай силову на ноги",
    "прибери завтрашню пробіжку",
    "зменш дистанцію в неділю",
])
def test_looks_like_plan_edit_matches_imperative_verbs(text):
    assert chat_router._looks_like_plan_edit(text) is True


@pytest.mark.parametrize("text", [
    "як мій сон цього тижня?",
    "що заплановано на завтра?",
    "чи варто бігти інтервали при такому пульсі?",
])
def test_looks_like_plan_edit_false_for_questions(text):
    assert chat_router._looks_like_plan_edit(text) is False


# ---------- GET /chat ----------

def test_chat_requires_login(client):
    assert client.get("/chat", follow_redirects=False).status_code == 303


def test_chat_page_renders_empty_state(auth_client):
    client, _ = auth_client
    r = client.get("/chat")
    assert r.status_code == 200
    assert "Ще нема повідомлень" in r.text


def test_chat_page_newest_first_and_load_more(auth_client):
    client, uid = auth_client

    async def seed():
        async with async_session_maker() as s:
            for i in range(35):
                await repository.log_report(
                    s, user_id=uid, kind="ask", model="claude-sonnet-5", ok=True,
                    question=f"питання номер {i}", report_text=f"відповідь {i}",
                )
    anyio.run(seed)

    body = client.get("/chat").text
    # newest exchange (34) appears before the older one (33) in the HTML → newest at top
    assert body.index("питання номер 34") < body.index("питання номер 33")
    # each turn carries a date/time label
    assert 'class="when"' in body
    # only the newest 30 are shown by default; #4 (35 - 31) is off the first page
    assert "питання номер 4" not in body
    # a "load more" link is offered because there are >30
    assert "Показати більше" in body and "/chat?limit=60" in body

    # loading more reveals the older ones
    more = client.get("/chat?limit=60").text
    assert "питання номер 0" in more


# ---------- POST /chat ----------

def test_chat_send_question_routes_to_run_ask(auth_client):
    client, uid = auth_client
    fake_ask = AsyncMock(return_value="Сон непоганий.")
    with patch.object(chat_router, "run_ask", fake_ask):
        r = client.post("/chat", data={"message": "як мій сон?"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/chat"
    fake_ask.assert_awaited_once()
    assert fake_ask.await_args.kwargs["user_id"] == uid
    assert fake_ask.await_args.args[1] == "як мій сон?"


def test_chat_send_plan_edit_sets_pending(auth_client):
    client, uid = auth_client
    plan = object()
    edit = PlanEdit(
        summary="Переніс довгу на суботу.",
        operations=[PlanOp(action="move", date="2026-07-01", to_date="2026-07-04")],
    )
    fake_edit = AsyncMock(return_value=(plan, edit))
    with patch.object(chat_router, "run_plan_edit", fake_edit):
        r = client.post(
            "/chat", data={"message": "перенеси довгу на суботу"}, follow_redirects=False
        )
    assert r.status_code == 303

    async def read():
        async with async_session_maker() as s:
            return await repository.get_pending_plan_edit(s, uid)

    pending = anyio.run(read)
    assert pending["summary"] == "Переніс довгу на суботу."
    assert pending["ops"][0]["action"] == "move"

    page = client.get("/chat")
    assert "Переніс довгу на суботу." in page.text


def test_chat_send_plan_edit_with_no_operations_leaves_no_pending(auth_client):
    client, uid = auth_client
    plan = object()
    edit = PlanEdit(summary="Не зрозумів, що змінити.", operations=[])
    with patch.object(chat_router, "run_plan_edit", AsyncMock(return_value=(plan, edit))):
        client.post("/chat", data={"message": "заміни щось незрозуміле"})

    async def read():
        async with async_session_maker() as s:
            return await repository.get_pending_plan_edit(s, uid)

    assert anyio.run(read) is None


def test_chat_send_analyst_error_flashes_query_param(auth_client):
    from app.analysis.client import AnalystError

    client, _ = auth_client
    with patch.object(chat_router, "run_ask", AsyncMock(side_effect=AnalystError("Немає плану."))):
        r = client.post("/chat", data={"message": "що по плану?"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/chat?err=")

    page = client.get(r.headers["location"])
    assert "Немає плану." in page.text


def test_chat_send_blank_message_is_a_noop(auth_client):
    client, _ = auth_client
    with patch.object(chat_router, "run_ask", AsyncMock()) as fake_ask:
        r = client.post("/chat", data={"message": "   "}, follow_redirects=False)
    assert r.status_code == 303
    fake_ask.assert_not_called()


# ---------- POST /chat/confirm ----------

def _seed_plan_with_workout(uid: int, date="2026-07-01"):
    async def seed():
        async with async_session_maker() as s:
            plan = TrainingPlan(user_id=uid, goal="general", status="active")
            s.add(plan)
            await s.flush()
            w = PlannedWorkout(plan_id=plan.id, user_id=uid, date=date, type="long",
                               dist_km=10.0, description="довгий біг", status="planned")
            s.add(w)
            await s.commit()
            return plan.id

    return anyio.run(seed)


def test_chat_confirm_apply_moves_workout_and_clears_pending(auth_client):
    client, uid = auth_client
    _seed_plan_with_workout(uid)

    async def stage():
        async with async_session_maker() as s:
            await repository.set_pending_plan_edit(
                s, uid,
                [{"action": "move", "date": "2026-07-01", "to_date": "2026-07-04"}], [],
                summary="Переніс довгу на п'ятницю.",
            )

    anyio.run(stage)

    r = client.post("/chat/confirm", data={"action": "apply"}, follow_redirects=False)
    assert r.status_code == 303

    async def read():
        async with async_session_maker() as s:
            pending = await repository.get_pending_plan_edit(s, uid)
            plan = await repository.get_active_plan(s, uid)
            ws = await repository.list_workouts(s, plan.id)
            return pending, {w.date for w in ws}

    pending, dates = anyio.run(read)
    assert pending is None
    assert "2026-07-04" in dates and "2026-07-01" not in dates


def test_chat_confirm_cancel_leaves_plan_untouched(auth_client):
    client, uid = auth_client
    _seed_plan_with_workout(uid)

    async def stage():
        async with async_session_maker() as s:
            await repository.set_pending_plan_edit(
                s, uid, [{"action": "move", "date": "2026-07-01", "to_date": "2026-07-04"}], [],
            )

    anyio.run(stage)
    r = client.post("/chat/confirm", data={"action": "cancel"}, follow_redirects=False)
    assert r.status_code == 303

    async def read():
        async with async_session_maker() as s:
            pending = await repository.get_pending_plan_edit(s, uid)
            plan = await repository.get_active_plan(s, uid)
            ws = await repository.list_workouts(s, plan.id)
            return pending, {w.date for w in ws}

    pending, dates = anyio.run(read)
    assert pending is None
    assert "2026-07-01" in dates  # untouched


def test_chat_confirm_with_no_pending_is_a_noop(auth_client):
    client, uid = auth_client
    _seed_plan_with_workout(uid)
    r = client.post("/chat/confirm", data={"action": "apply"}, follow_redirects=False)
    assert r.status_code == 303
