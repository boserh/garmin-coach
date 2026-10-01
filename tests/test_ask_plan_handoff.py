"""The coach (/ask) hands a requested plan change to the plan editor.

The web chat routes a message to the editor only on a few imperative words, so «можеш
поміняти сьогоднішнє тренування» used to reach the coach — which could only say "change
it yourself". Now the coach has ``propose_plan_change``: it records the request, answers,
and the caller runs the usual proposal with ✅/❌. Every Claude call is faked ($0).
"""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

from app.analysis import reports
from app.analysis.client import AnalystError, CallStats
from app.analysis.prompts import ASK_PLAN_CHANGE_SECTION, SYSTEM_ASK_TOOLS
from app.core.crypto import hash_password
from app.db import users
from app.db.base import async_session_maker
from app.garmin import repository
from app.garmin.schemas import PlanEdit, PlanOp
from app.main import create_app
from app.routers import chat as chat_router
from tests.web_helpers import wait_live_jobs

# ---------- the agent ----------


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool(name, **args):
    return SimpleNamespace(type="tool_use", id=f"t-{name}", name=name, input=args)


def _scripted(*messages, seen=None):
    """A stand-in for one /ask round per call, recording what each round was sent."""
    it = iter(messages)

    def fake(model, system, msgs, tools, api_key, max_tokens, on_text=None):
        if seen is not None:
            seen.append({"system": system, "tools": [t["name"] for t in tools],
                         "messages": list(msgs)})
        return next(it), CallStats(kind="ask", model=model)
    return fake


async def test_the_tool_is_offered_only_when_the_caller_can_act_on_it(session, monkeypatch):
    seen = []
    monkeypatch.setattr(reports, "_complete_tools", _scripted(
        SimpleNamespace(stop_reason="end_turn", content=[_text("ок")]),
        SimpleNamespace(stop_reason="end_turn", content=[_text("ок")]), seen=seen))
    await reports.run_ask_agent(session, 1, "?", [], [], None)
    await reports.run_ask_agent(session, 1, "?", [], [], None, on_plan_change=lambda i: None)
    without, with_ = seen
    assert "propose_plan_change" not in without["tools"]
    assert without["system"] == SYSTEM_ASK_TOOLS
    assert "propose_plan_change" in with_["tools"]
    assert with_["system"].endswith(ASK_PLAN_CHANGE_SECTION)


async def test_a_change_request_is_handed_over_and_the_coach_still_answers(session, monkeypatch):
    seen = []
    monkeypatch.setattr(reports, "_complete_tools", _scripted(
        SimpleNamespace(stop_reason="tool_use", content=[
            _tool("propose_plan_change", instruction="Зроби сьогоднішнє тренування легким")]),
        SimpleNamespace(stop_reason="end_turn",
                        content=[_text("Передав — пропозиція зʼявиться з кнопками.")]),
        seen=seen))
    handed = []
    text, _stats, rounds = await reports.run_ask_agent(
        session, 1, "можеш поміняти сьогоднішнє тренування", [], [], None,
        on_plan_change=handed.append)
    assert handed == ["Зроби сьогоднішнє тренування легким"]
    assert text == "Передав — пропозиція зʼявиться з кнопками." and rounds == 2
    # the model was told it's handed over and that the proposal follows its reply
    result = seen[1]["messages"][-1]["content"][0]
    assert json.loads(result["content"]) == reports.PLAN_CHANGE_TOOL_RESULT


async def test_an_empty_instruction_hands_nothing_over(session, monkeypatch):
    seen = []
    monkeypatch.setattr(reports, "_complete_tools", _scripted(
        SimpleNamespace(stop_reason="tool_use",
                        content=[_tool("propose_plan_change", instruction="  ")]),
        SimpleNamespace(stop_reason="end_turn", content=[_text("Що саме змінити?")]),
        seen=seen))
    handed = []
    await reports.run_ask_agent(session, 1, "?", [], [], None, on_plan_change=handed.append)
    assert handed == []
    assert "error" in json.loads(seen[1]["messages"][-1]["content"][0]["content"])


async def test_a_handed_over_answer_is_never_served_from_the_cache(session, monkeypatch):
    async def agent(session, user_id, question, reports_, recent_asks, api_key,
                    on_plan_change=None):
        on_plan_change("Перенеси довгу на суботу")
        return "Передав.", CallStats(kind="ask", model="m"), 2

    monkeypatch.setattr(reports, "run_ask_agent", agent)
    put = AsyncMock()
    monkeypatch.setattr("app.db.llm_cache.put", put)
    handed = []
    assert await reports.run_ask(session, "перенеси довгу", user_id=1,
                                 on_plan_change=handed.append) == "Передав."
    assert handed == ["Перенеси довгу на суботу"]
    put.assert_not_called()


def test_the_prompt_never_sends_the_athlete_to_edit_garmin_themselves():
    flat = " ".join(ASK_PLAN_CHANGE_SECTION.split())
    assert "Ніколи не кажи, що змінювати треба самому в Garmin" in flat
    assert "✅" in ASK_PLAN_CHANGE_SECTION


# ---------- web chat ----------

@pytest.fixture
def web(request):
    email = f"{request.node.name}@example.com"
    with TestClient(create_app()) as c:
        async def seed():
            async with async_session_maker() as s:
                u = await users.get_by_email(s, email) or await users.create_user(
                    s, email=email, password_hash=hash_password("pw"), is_admin=False)
                return u.id
        uid = anyio.run(seed)
        assert c.post("/login", data={"email": email, "password": "pw"}).status_code == 200
        yield c, uid


def _handing_ask(instruction, reply="Передав у редактор плану."):
    async def fake(session, question, *, user_id=None, api_key=None, on_event=None,
                   on_plan_change=None):
        on_plan_change(instruction)
        return reply
    return fake


def _pending(uid):
    async def read():
        async with async_session_maker() as s:
            return await repository.get_pending_plan_edit(s, uid)
    return anyio.run(read)


def test_a_change_asked_in_plain_words_ends_with_the_proposal_card(web):
    client, uid = web
    assert not chat_router._looks_like_plan_edit("можеш поміняти сьогоднішнє тренування")
    edit = PlanEdit(summary="Сьогодні легкий біг замість темпового.",
                    operations=[PlanOp(action="modify", date="2026-10-01", type="easy")])
    fake_edit = AsyncMock(return_value=(object(), edit))
    with patch.object(chat_router, "run_ask", _handing_ask("Зроби сьогоднішнє легким")), \
            patch.object(chat_router, "run_plan_edit", fake_edit):
        r = client.post("/chat", data={"message": "можеш поміняти сьогоднішнє тренування"},
                        headers={"Accept": "application/json"})
        wait_live_jobs()
        from app import livejobs
        job = livejobs.get(r.json()["job"], uid)
    # the editor got the coach's self-contained instruction, not the raw question
    assert fake_edit.await_args.kwargs["instruction"] == "Зроби сьогоднішнє легким"
    name, done = job.events[-1]
    assert name == "done" and done["reply"] == "Передав у редактор плану."
    assert "Сьогодні легкий біг замість темпового." in done["card"]
    assert _pending(uid)["ops"][0]["action"] == "modify"


def test_a_question_that_hands_nothing_over_never_touches_the_editor(web):
    client, uid = web

    async def plain(session, question, *, user_id=None, api_key=None, on_event=None,
                    on_plan_change=None):
        return "Сон стабільний."

    with patch.object(chat_router, "run_ask", plain), \
            patch.object(chat_router, "run_plan_edit", AsyncMock()) as fake_edit:
        client.post("/chat", data={"message": "як мій сон?"})
        wait_live_jobs()
    fake_edit.assert_not_called()
    assert _pending(uid) is None


def test_no_plan_to_change_keeps_the_answer_and_says_why(web):
    client, uid = web
    with patch.object(chat_router, "run_ask", _handing_ask("Зроби сьогодні легше")), \
            patch.object(chat_router, "run_plan_edit",
                         AsyncMock(side_effect=AnalystError("Немає активної програми."))):
        r = client.post("/chat", data={"message": "зроби сьогодні легше"},
                        headers={"Accept": "application/json"})
        wait_live_jobs()
        from app import livejobs
        job = livejobs.get(r.json()["job"], uid)
    assert job.events[-1] == ("done", {
        "reply": "Передав у редактор плану.\n\nНемає активної програми."})


# ---------- bot /ask ----------

class _FakeMessage:
    def __init__(self, chat_id=777):
        self.chat = SimpleNamespace(id=chat_id)
        self.chat_id = chat_id
        self.message_id = 1
        self.text = ""
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))
        return SimpleNamespace(chat_id=self.chat_id, message_id=len(self.replies) + 1)


async def test_bot_ask_answers_then_shows_the_proposal(session, monkeypatch):
    from app.db.models import User
    from bot import handlers as h

    u = User(email="ask-handoff@e.com", password_hash="h", is_approved=True,
             is_active=True, telegram_chat_id=777)
    session.add(u)
    await session.commit()

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(h, "async_session_maker", maker)
    monkeypatch.setattr(h, "run_ask", _handing_ask("Зроби сьогоднішнє легким",
                                                   reply="Передав."))
    order = []
    msg = _FakeMessage()

    async def fake_plan_edit(update, ctx, instruction):
        order.append(("edit", instruction, len(msg.replies)))

    monkeypatch.setattr(h, "_plan_edit", fake_plan_edit)
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=777))
    await h.ask(update, SimpleNamespace(args=["поміняй", "сьогоднішнє"]))
    assert msg.replies[-1][0] == "Передав."
    # the coach's answer is sent before the proposal follows it
    assert order == [("edit", "Зроби сьогоднішнє легким", 2)]
