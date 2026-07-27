"""ST-23: dialogue about an unconfirmed plan proposal — ask a question about it (the
proposal stays on the table) or correct it (a brand-new proposal replaces it).

Covers the three layers: the pending-state extras (thread/instruction/message), the
engine's ``pending`` context in ``run_plan_edit``, and both front-ends (the bot's
``/plan <text>``/plain-text follow-up and the web chat's ``refine=1`` input).
"""
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.analysis import plans
from app.analysis.client import CallStats
from app.db.models import ReportLog, TrainingPlan, User
from app.garmin import repository
from app.garmin.repository.state import PENDING_THREAD_MAX
from app.garmin.schemas import PlanEdit, PlanOp

U1 = 1


def _iso(delta_days: int) -> str:
    return (dt.date.today() + dt.timedelta(days=delta_days)).isoformat()


# ---------- pending state extras ----------

async def test_set_pending_stores_dialogue_extras(session):
    await repository.set_pending_plan_edit(
        session, U1, [{"action": "skip", "date": _iso(1)}], [],
        summary="пропускаю", instruction="прибери завтрашню",
        thread=[{"q": "чому?", "a": "бо втома"}], message={"chat_id": 5, "message_id": 9},
    )
    pending = await repository.get_pending_plan_edit(session, U1)
    assert pending["instruction"] == "прибери завтрашню"
    assert pending["thread"] == [{"q": "чому?", "a": "бо втома"}]
    assert pending["message"] == {"chat_id": 5, "message_id": 9}


async def test_old_style_pending_without_extras_still_reads(session):
    """A proposal written before ST-23 (or by /sick with no extras) must stay usable —
    the dialogue fields are all optional on read."""
    await repository.set_pending_plan_edit(session, U1, [{"action": "skip", "date": _iso(1)}])
    pending = await repository.get_pending_plan_edit(session, U1)
    assert pending["ops"] and pending.get("thread") == [] and pending.get("instruction") is None


def test_append_thread_trims_to_cap():
    pending = {"thread": [{"q": f"q{i}", "a": f"a{i}"} for i in range(PENDING_THREAD_MAX)]}
    out = repository.append_thread(pending, "новий", "відповідь")
    assert len(out) == PENDING_THREAD_MAX
    assert out[-1] == {"q": "новий", "a": "відповідь"}
    assert out[0]["q"] == "q1"          # the oldest turn dropped out


def test_append_thread_from_empty_pending():
    assert repository.append_thread(None, "питання", None) == [{"q": "питання", "a": ""}]


# ---------- engine: run_plan_edit(pending=...) ----------

async def _seed_plan(session):
    plan = TrainingPlan(user_id=U1, goal="general", status="active")
    session.add(plan)
    await session.flush()
    from app.db.models import PlannedWorkout
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date=_iso(2), type="long",
                               dist_km=18.0, description="довгий", status="planned"))
    await session.commit()
    return plan


async def test_run_plan_edit_feeds_pending_into_the_prompt_context(session):
    await _seed_plan(session)
    seen = {}

    def fake(context, api_key=None):
        seen.update(context)
        return PlanEdit(summary="", operations=[], answer="Бо в суботу дощ."), \
            CallStats(kind="plan_edit", model="m")

    pending = {"ops": [{"action": "move", "date": _iso(2), "to_date": _iso(4)}],
               "summary": "переніс довгий на суботу", "instruction": "перенеси довгий",
               "thread": [{"q": "а темп?", "a": "легкий"}]}
    with patch.object(plans, "plan_edit_with_stats", fake):
        _plan, edit = await plans.run_plan_edit(
            session, user_id=U1, instruction="чому саме субота?", pending=pending,
        )
    assert seen["instruction"] == "чому саме субота?"
    assert seen["pending"]["operations"] == pending["ops"]
    assert seen["pending"]["summary"] == "переніс довгий на суботу"
    assert seen["pending"]["thread"] == [{"q": "а темп?", "a": "легкий"}]
    assert edit.answer == "Бо в суботу дощ." and edit.operations == []


async def test_run_plan_edit_without_pending_has_no_pending_key(session):
    await _seed_plan(session)
    seen = {}

    def fake(context, api_key=None):
        seen.update(context)
        return PlanEdit(summary="ок", operations=[PlanOp(action="skip", date=_iso(2))]), \
            CallStats(kind="plan_edit", model="m")

    with patch.object(plans, "plan_edit_with_stats", fake):
        await plans.run_plan_edit(session, user_id=U1, instruction="прибери довгий")
    assert "pending" not in seen


async def test_question_turn_logs_the_answer_as_the_reply(session):
    await _seed_plan(session)
    edit = PlanEdit(summary="", operations=[], answer="Бо в неділю довгий.")
    with patch.object(plans, "plan_edit_with_stats",
                      return_value=(edit, CallStats(kind="plan_edit", model="m"))):
        await plans.run_plan_edit(
            session, user_id=U1, instruction="чому не в неділю?",
            pending={"ops": [{"action": "skip", "date": _iso(2)}]},
        )
    row = (await session.execute(
        select(ReportLog).where(ReportLog.kind == "plan_edit")
        .order_by(ReportLog.id.desc())
    )).scalars().first()
    # marked as a follow-up in the transcript, and the answer is what the user reads back
    assert row.question.startswith("↳ ")
    assert row.report_text == "Бо в неділю довгий."


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


class _FakeBot:
    def __init__(self):
        self.retired = []

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.retired.append((chat_id, message_id, reply_markup))


@pytest.fixture
def bot_env(session, monkeypatch):
    """Bot handlers against the test session, with the Garmin runtime stubbed out."""
    from bot import handlers as h

    @asynccontextmanager
    async def maker():
        yield session

    @asynccontextmanager
    async def runtime(_session, _user):
        yield SimpleNamespace(anthropic_key=None)

    monkeypatch.setattr(h, "async_session_maker", maker)
    monkeypatch.setattr(h, "user_runtime", runtime)
    return h


async def _mk_user(session, chat_id=555):
    u = User(email=f"{chat_id}@e.com", password_hash="h", is_approved=True,
             is_active=True, telegram_chat_id=chat_id)
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


async def test_bot_first_proposal_stores_message_ref_and_hint(bot_env, session):
    h = bot_env
    user = await _mk_user(session)
    edit = PlanEdit(summary="Переніс довгий на суботу.",
                    operations=[PlanOp(action="move", date=_iso(2), to_date=_iso(4))])
    msg = _FakeMessage("перенеси довгий на суботу")
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=555))
    with patch.object(h, "run_plan_edit", AsyncMock(return_value=(object(), edit))):
        await h._plan_edit(update, SimpleNamespace(bot=_FakeBot()), "перенеси довгий на суботу")

    text, kb = msg.replies[-1]
    assert "Переніс довгий на суботу." in text
    assert "Питання чи корекція" in text and kb is not None
    pending = await repository.get_pending_plan_edit(session, user.id)
    assert pending["ops"][0]["action"] == "move"
    assert pending["instruction"] == "перенеси довгий на суботу"
    assert pending["message"]["chat_id"] == 555 and pending["message"]["message_id"]


async def test_bot_followup_question_keeps_the_proposal_and_grows_the_thread(bot_env, session):
    h = bot_env
    user = await _mk_user(session, chat_id=556)
    await repository.set_pending_plan_edit(
        session, user.id, [{"action": "move", "date": _iso(2), "to_date": _iso(4)}], [],
        summary="Переніс довгий на суботу.", instruction="перенеси довгий",
        message={"chat_id": 556, "message_id": 42},
    )
    edit = PlanEdit(summary="", operations=[], answer="Бо в неділю вітер.")
    msg = _FakeMessage("а чому саме субота?", chat_id=556)
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=556))
    bot = _FakeBot()
    with patch.object(h, "run_plan_edit", AsyncMock(return_value=(object(), edit))) as fake:
        await h._plan_edit(update, SimpleNamespace(bot=bot), "а чому саме субота?")

    # the pending proposal rode into the engine as context
    assert fake.await_args.kwargs["pending"]["summary"] == "Переніс довгий на суботу."
    # the answer comes back WITH the proposal restated + live buttons
    text, kb = msg.replies[-1]
    assert text.startswith("Бо в неділю вітер.")
    assert "Переніс довгий на суботу." in text and kb is not None
    # the previous message's keyboard was retired, so only one button set stays live
    assert bot.retired == [(556, 42, None)]

    pending = await repository.get_pending_plan_edit(session, user.id)
    assert pending["ops"][0]["action"] == "move"          # untouched
    assert pending["summary"] == "Переніс довгий на суботу."
    assert pending["thread"][-1]["q"] == "а чому саме субота?"
    assert pending["thread"][-1]["a"] == "Бо в неділю вітер."
    assert pending["message"]["message_id"] != 42          # now points at the new message


async def test_bot_followup_correction_replaces_the_proposal(bot_env, session):
    h = bot_env
    user = await _mk_user(session, chat_id=557)
    await repository.set_pending_plan_edit(
        session, user.id, [{"action": "move", "date": _iso(2), "to_date": _iso(4)}], [],
        summary="Переніс довгий на суботу.", instruction="перенеси довгий",
    )
    edit = PlanEdit(summary="Переніс довгий на неділю.",
                    operations=[PlanOp(action="move", date=_iso(2), to_date=_iso(5))],
                    answer="Ок, не субота, а неділя.")
    msg = _FakeMessage("краще неділя", chat_id=557)
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=557))
    with patch.object(h, "run_plan_edit", AsyncMock(return_value=(object(), edit))):
        await h._plan_edit(update, SimpleNamespace(bot=_FakeBot()), "краще неділя")

    pending = await repository.get_pending_plan_edit(session, user.id)
    assert pending["ops"][0]["to_date"] == _iso(5)         # the new proposal, whole
    assert pending["summary"] == "Переніс довгий на неділю."
    assert pending["instruction"] == "перенеси довгий"     # the dialogue's root request
    assert pending["thread"][-1]["q"] == "краще неділя"
    text, _kb = msg.replies[-1]
    assert text.startswith("Ок, не субота, а неділя.")


async def test_bot_no_pending_and_no_operations_replies_without_storing(bot_env, session):
    h = bot_env
    user = await _mk_user(session, chat_id=558)
    edit = PlanEdit(summary="Не зрозумів, що змінити.", operations=[])
    msg = _FakeMessage("щось незрозуміле", chat_id=558)
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=558))
    with patch.object(h, "run_plan_edit", AsyncMock(return_value=(object(), edit))):
        await h._plan_edit(update, SimpleNamespace(bot=_FakeBot()), "щось незрозуміле")
    assert msg.replies[-1] == ("Не зрозумів, що змінити.", None)
    assert await repository.get_pending_plan_edit(session, user.id) is None


async def test_plan_followup_ignores_text_without_a_pending_proposal(bot_env, session):
    h = bot_env
    await _mk_user(session, chat_id=559)
    msg = _FakeMessage("просто привіт", chat_id=559)
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=559))
    with patch.object(h, "run_plan_edit", AsyncMock()) as fake:
        await h.plan_followup(update, SimpleNamespace(bot=_FakeBot()))
    fake.assert_not_called()
    assert msg.replies == []      # silent, exactly as before ST-23


async def test_plan_followup_routes_into_the_edit_engine_when_pending(bot_env, session):
    h = bot_env
    user = await _mk_user(session, chat_id=560)
    await repository.set_pending_plan_edit(
        session, user.id, [{"action": "skip", "date": _iso(2)}], [], summary="Пропускаю довгий.",
    )
    edit = PlanEdit(summary="", operations=[], answer="Бо HRV просів.")
    msg = _FakeMessage("чому?", chat_id=560)
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=560))
    with patch.object(h, "run_plan_edit", AsyncMock(return_value=(object(), edit))) as fake:
        await h.plan_followup(update, SimpleNamespace(bot=_FakeBot()))
    fake.assert_awaited_once()
    assert fake.await_args.kwargs["instruction"] == "чому?"
    assert msg.replies[-1][0].startswith("Бо HRV просів.")


async def test_plan_followup_ignores_an_unknown_chat(bot_env, session):
    h = bot_env
    msg = _FakeMessage("привіт", chat_id=99999)
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=99999))
    with patch.object(h, "run_plan_edit", AsyncMock()) as fake:
        await h.plan_followup(update, SimpleNamespace(bot=_FakeBot()))
    fake.assert_not_called()
    assert msg.replies == []
