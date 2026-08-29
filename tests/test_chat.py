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


def test_chat_page_reads_like_a_chat_and_loads_older(auth_client):
    """Oldest at the top, newest at the bottom, composer under the thread.

    The window is still taken newest-first off the end (that's the efficient query), but
    the page reverses it: rendering the query order straight through produced a
    reverse-chronological feed where an answer sat above the question before it."""
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
    # …33 before 34: time runs downward, like every chat.
    assert body.index("питання номер 33") < body.index("питання номер 34")
    # A turn's own answer still follows its question.
    assert body.index("питання номер 34") < body.index("відповідь 34")
    # The newest turn is the last thing in the thread, and the composer comes after it.
    assert body.index("питання номер 34") < body.index('name="message"')
    # each turn carries a date/time label
    assert 'class="when"' in body
    # only the newest 30 are shown by default; #4 (35 - 31) is off the first page
    assert "питання номер 4" not in body
    # older ones live above, so the link that fetches them says so and sits above the thread
    assert "Показати старіші" in body and "/chat?limit=60" in body
    assert body.index("Показати старіші") < body.index("питання номер 33")

    # loading more reveals the older ones
    more = client.get("/chat?limit=60").text
    assert "питання номер 0" in more
    assert more.index("питання номер 0") < more.index("питання номер 34")


def test_the_page_opens_at_the_newest_turn_but_not_after_load_more(auth_client):
    """A chat opens at the bottom. After "Показати старіші" it must not — the reader
    just asked to look at old messages and would be yanked straight back down."""
    client, uid = auth_client

    async def seed():
        async with async_session_maker() as s:
            for i in range(35):
                await repository.log_report(
                    s, user_id=uid, kind="ask", model="claude-sonnet-5", ok=True,
                    question=f"q{i}", report_text=f"a{i}",
                )
    anyio.run(seed)

    assert 'data-jump-latest="1"' in client.get("/chat").text
    assert 'data-jump-latest' not in client.get("/chat?limit=60").text


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


# ---------- ST-23: dialogue about a pending proposal ----------

def _stage_pending(uid, **kw):
    async def stage():
        async with async_session_maker() as s:
            await repository.set_pending_plan_edit(
                s, uid, [{"action": "move", "date": "2026-07-01", "to_date": "2026-07-04"}], [],
                summary="Переніс довгу на суботу.", instruction="перенеси довгу", **kw,
            )

    anyio.run(stage)


def _read_pending(uid):
    async def read():
        async with async_session_maker() as s:
            return await repository.get_pending_plan_edit(s, uid)

    return anyio.run(read)


def test_chat_refine_question_keeps_the_proposal_and_grows_the_thread(auth_client):
    client, uid = auth_client
    _stage_pending(uid)
    edit = PlanEdit(summary="", operations=[], answer="Бо в неділю довгий.")
    fake = AsyncMock(return_value=(object(), edit))
    with patch.object(chat_router, "run_plan_edit", fake):
        r = client.post("/chat", data={"message": "чому саме субота?", "refine": "1"},
                        follow_redirects=False)
    assert r.status_code == 303
    # the pending proposal rode into the engine as context
    assert fake.await_args.kwargs["pending"]["summary"] == "Переніс довгу на суботу."

    pending = _read_pending(uid)
    assert pending["ops"][0]["to_date"] == "2026-07-04"      # proposal untouched
    assert pending["thread"][-1] == {"q": "чому саме субота?", "a": "Бо в неділю довгий."}
    assert "чому саме субота?" in client.get("/chat").text   # thread rendered on the card


def test_chat_refine_correction_replaces_the_proposal(auth_client):
    client, uid = auth_client
    _stage_pending(uid)
    edit = PlanEdit(
        summary="Переніс довгу на неділю.", answer="Ок, неділя.",
        operations=[PlanOp(action="move", date="2026-07-01", to_date="2026-07-05")],
    )
    with patch.object(chat_router, "run_plan_edit", AsyncMock(return_value=(object(), edit))):
        client.post("/chat", data={"message": "краще неділя", "refine": "1"})

    pending = _read_pending(uid)
    assert pending["ops"][0]["to_date"] == "2026-07-05"
    assert pending["summary"] == "Переніс довгу на неділю."
    assert pending["instruction"] == "перенеси довгу"        # the dialogue's root request
    assert pending["thread"][-1]["q"] == "краще неділя"


def test_chat_main_composer_still_answers_questions_while_a_proposal_waits(auth_client):
    """Only the proposal card's own input refines; the main composer keeps routing by the
    heuristic, so an unrelated question isn't swallowed by the plan-edit engine."""
    client, uid = auth_client
    _stage_pending(uid)
    fake_ask = AsyncMock(return_value="Сон непоганий.")
    with patch.object(chat_router, "run_ask", fake_ask):
        client.post("/chat", data={"message": "як мій сон?"})
    fake_ask.assert_awaited_once()
    assert _read_pending(uid)["ops"]        # proposal untouched


def test_chat_refine_without_a_pending_proposal_falls_back_to_the_heuristic(auth_client):
    client, uid = auth_client
    fake_ask = AsyncMock(return_value="Відповідь.")
    with patch.object(chat_router, "run_ask", fake_ask):
        client.post("/chat", data={"message": "як мій сон?", "refine": "1"})
    fake_ask.assert_awaited_once()          # stale card, no pending → plain question
    assert _read_pending(uid) is None


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


def test_chat_confirm_without_an_action_keeps_the_proposal_and_explains(auth_client):
    """The confirm buttons carry the choice as the submitter's name/value, so a client
    that drops it (a stale cached app.js) posts an empty body. That must never be read as
    "apply", and must never be answered with FastAPI's raw 422 JSON in place of the chat."""
    client, uid = auth_client
    _seed_plan_with_workout(uid)
    _stage_pending(uid)

    r = client.post("/chat/confirm", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/chat?err=")

    pending = _read_pending(uid)
    assert pending is not None and pending["ops"][0]["to_date"] == "2026-07-04"

    async def read_dates():
        async with async_session_maker() as s:
            plan = await repository.get_active_plan(s, uid)
            return {w.date for w in await repository.list_workouts(s, plan.id)}

    assert "2026-07-01" in anyio.run(read_dates)          # nothing applied
    assert chat_router.CONFIRM_NO_ACTION_MSG in client.get(r.headers["location"]).text


def test_chat_confirm_with_an_unknown_action_is_refused(auth_client):
    client, uid = auth_client
    _seed_plan_with_workout(uid)
    _stage_pending(uid)
    r = client.post("/chat/confirm", data={"action": "nonsense"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/chat?err=")
    assert _read_pending(uid) is not None


# ---------- the plan edit runs with THIS user's Garmin provider bound ----------

def test_chat_plan_edit_binds_the_users_garmin_provider(auth_client):
    """run_plan_edit reads the plan's strength templates off Garmin. Unbound, that fell
    through to the legacy .env single-user provider — `KeyError: 'GARMIN_EMAIL'` on this
    machine, and the *seed account's* workouts on a machine where .env still has them."""
    from app.garmin import providers

    client, uid = auth_client
    seen = {}

    async def fake_edit(session, **kw):
        seen["provider"] = providers.get_provider()
        return object(), PlanEdit(summary="Переніс.", operations=[
            PlanOp(action="move", date="2026-07-01", to_date="2026-07-04")])

    with patch.object(chat_router, "run_plan_edit", fake_edit):
        r = client.post("/chat", data={"message": "перенеси довгу на суботу"},
                        follow_redirects=False)
    assert r.status_code == 303
    assert seen["provider"] is not None
    # the per-user provider, carrying this account's credentials — not the global one
    assert getattr(seen["provider"], "_creds", None) is not None
    assert seen["provider"]._creds.user_id == uid
    assert seen["provider"] is not providers._default_provider()


def test_chat_plan_edit_survives_invalid_garmin_credentials(auth_client):
    """A Garmin link that is known-broken must cost the edit its exercise detail, not the
    whole proposal — and must not silently borrow the .env account instead."""
    from app.garmin import providers

    client, uid = auth_client

    async def mark_invalid():
        async with async_session_maker() as s:
            u = await users.get_by_id(s, uid)
            u.garmin_creds_invalid = True
            await s.commit()

    anyio.run(mark_invalid)
    seen = {}

    async def fake_edit(session, **kw):
        provider = providers.get_provider()
        seen["provider"] = provider
        with pytest.raises(providers.GarminUnavailable):
            provider.connectapi("/workout-service/workout/1")
        return object(), PlanEdit(summary="Переніс.", operations=[
            PlanOp(action="move", date="2026-07-01", to_date="2026-07-04")])

    with patch.object(chat_router, "run_plan_edit", fake_edit):
        r = client.post("/chat", data={"message": "перенеси довгу на суботу"},
                        follow_redirects=False)
    assert r.status_code == 303                      # not the 409 creds-invalid page
    assert seen["provider"] is not providers._default_provider()
    assert _read_pending(uid)["summary"] == "Переніс."
