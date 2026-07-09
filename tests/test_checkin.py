"""EP-12 post-run check-in: subjective storage (RPE + pain), the bot callback/command
flow, and that the check-in feeds the /activity payload + dedup-cache key."""
from types import SimpleNamespace

import pytest

from app.analysis.service import _activity_cache_key, activity_payload
from app.db.models import ActivityRecord, User
from app.garmin import repository

U1 = 1


async def _mk_activity(session, **kw):
    defaults = dict(user_id=U1, activity_id=111, date="2026-07-08", type="running",
                    dist_km=5.0, dur_min=30.0)
    act = ActivityRecord(**{**defaults, **kw})
    session.add(act)
    await session.commit()
    await session.refresh(act)
    return act


# --- repository.set_subjective ------------------------------------------------

async def test_set_subjective_rpe_then_note_merges(session):
    act = await _mk_activity(session)
    await repository.set_subjective(session, U1, act.id, rpe=7)
    await session.commit()
    assert act.subjective == {"rpe": 7}

    # A later tap adds the niggle without dropping the RPE (merge, not overwrite-all).
    await repository.set_subjective(session, U1, act.id, note="коліно")
    await session.commit()
    assert act.subjective == {"rpe": 7, "pain": True, "note": "коліно"}


async def test_set_subjective_rpe_retap_overwrites(session):
    act = await _mk_activity(session)
    await repository.set_subjective(session, U1, act.id, rpe=5)
    await repository.set_subjective(session, U1, act.id, rpe=9)
    await session.commit()
    assert act.subjective["rpe"] == 9


async def test_set_subjective_no_pain_clears_note(session):
    act = await _mk_activity(session)
    await repository.set_subjective(session, U1, act.id, note="стопа")
    await repository.set_subjective(session, U1, act.id, pain=False)
    await session.commit()
    assert act.subjective == {"pain": False}


async def test_set_subjective_wrong_user_returns_none(session):
    act = await _mk_activity(session)
    assert await repository.set_subjective(session, 999, act.id, rpe=6) is None


async def test_get_last_activity_newest_first(session):
    older = await _mk_activity(session, activity_id=1, date="2026-07-01")
    newer = await _mk_activity(session, activity_id=2, date="2026-07-08")
    got = await repository.get_last_activity(session, U1)
    assert got.id == newer.id
    assert older.id != newer.id


# --- payload / cache key ------------------------------------------------------

def test_subjective_enters_activity_payload_and_cache_key():
    base = SimpleNamespace(type="running", date="2026-07-08", dur_min=30.0, dist_km=5.0,
                           avg_hr=140, max_hr=155, load=80.0, exercises=None, series=None,
                           subjective=None)
    with_rpe = SimpleNamespace(**{**base.__dict__, "subjective": {"rpe": 8, "pain": False}})

    assert "subjective" not in activity_payload(base)
    assert activity_payload(with_rpe)["subjective"] == {"rpe": 8, "pain": False}

    # RPE must move the dedup-cache key so the analysis isn't served stale (README pitfall).
    k_base = _activity_cache_key(activity_payload(base), "m")
    k_rpe = _activity_cache_key(activity_payload(with_rpe), "m")
    assert k_base != k_rpe


# --- bot callback + command ---------------------------------------------------

class _FakeQuery:
    def __init__(self, data, chat_id=555, text="аналіз"):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), text=text)
        self.edited = None

    async def answer(self):
        pass

    async def edit_message_text(self, text, reply_markup=None):
        self.edited = (text, reply_markup)


class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


async def _mk_user(session, chat_id=555):
    u = User(email=f"{chat_id}@e.com", password_hash="h", is_approved=True,
             is_active=True, telegram_chat_id=chat_id)
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest.fixture
def _single_session(session, monkeypatch):
    """Route the bot handlers' async_session_maker to the test session."""
    from contextlib import asynccontextmanager

    from bot import handlers as h

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(h, "async_session_maker", maker)
    return session


async def test_checkin_callback_rpe_stores_and_offers_pain(_single_session):
    from bot import handlers as h

    user = await _mk_user(_single_session)
    act = ActivityRecord(user_id=user.id, activity_id=1, date="2026-07-08", type="running")
    _single_session.add(act)
    await _single_session.commit()
    await _single_session.refresh(act)

    q = _FakeQuery(f"ci:rpe:{act.id}:7")
    await h.checkin_callback(SimpleNamespace(callback_query=q), None)

    await _single_session.refresh(act)
    assert act.subjective == {"rpe": 7}
    text, kb = q.edited
    assert "RPE 7/10" in text
    assert kb is not None            # pain picker still offered


async def test_checkin_callback_part_records_note_and_removes_kb(_single_session):
    from bot import handlers as h

    user = await _mk_user(_single_session)
    act = ActivityRecord(user_id=user.id, activity_id=1, date="2026-07-08", type="running")
    _single_session.add(act)
    await _single_session.commit()
    await _single_session.refresh(act)

    q = _FakeQuery(f"ci:part:{act.id}:knee")
    await h.checkin_callback(SimpleNamespace(callback_query=q), None)

    await _single_session.refresh(act)
    assert act.subjective == {"pain": True, "note": "коліно"}
    text, kb = q.edited
    assert "коліно" in text
    assert kb is None                # buttons quietly disappear after the answer


async def test_checkin_command_sets_rpe_on_last_activity(_single_session):
    from bot import handlers as h

    user = await _mk_user(_single_session)
    act = ActivityRecord(user_id=user.id, activity_id=1, date="2026-07-08", type="running")
    _single_session.add(act)
    await _single_session.commit()
    await _single_session.refresh(act)

    msg = _FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=user.telegram_chat_id), message=msg)
    ctx = SimpleNamespace(args=["8", "коліно"])
    await h.checkin(update, ctx)

    await _single_session.refresh(act)
    assert act.subjective == {"rpe": 8, "pain": True, "note": "коліно"}
    assert "RPE 8/10" in msg.replies[-1][0]


async def test_checkin_command_no_args_offers_keyboard(_single_session):
    from bot import handlers as h

    user = await _mk_user(_single_session)
    act = ActivityRecord(user_id=user.id, activity_id=1, date="2026-07-08", type="running",
                         dist_km=5.0)
    _single_session.add(act)
    await _single_session.commit()
    await _single_session.refresh(act)

    msg = _FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=user.telegram_chat_id), message=msg)
    await h.checkin(update, SimpleNamespace(args=[]))

    text, kb = msg.replies[-1]
    assert kb is not None            # RPE keyboard offered
    assert "Як пройшло" in text
